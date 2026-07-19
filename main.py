"""转生模拟器 AstrBot 插件 - 主入口
- 模式 A: 纯叙事(默认)
- 模式 B: 游戏世界 RPG(HP/等级/装备/经验) — 来自 rpg_tools.RPGMixin
- 模式 C: DND 跑团(RPG + D20 骰子) — 来自 dice.DiceMixin
- 独立上下文: 叙事历史 KV 存储 + 显式 contexts
- 4 个指令: /创建 /do /进度 /删除
"""

import asyncio
import time
import json
import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import (
    AssistantMessageSegment,
    ToolCallMessageSegment,
    UserMessageSegment,
    TextPart,
    ImageURLPart,
    ThinkPart,
    ToolCall,
    bind_checkpoint_messages,
)
from astrbot.core.agent.tool import ToolSet
from astrbot.core.message.components import Image
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.utils.quoted_message.extractor import QuotedMessageExtractor

from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.provider.func_tool_manager import PY_TO_JSON_TYPE
from astrbot.core.star.star_tools import StarTools

import docstring_parser

from .dice import DiceMixin
from .prompts import (
    HELP_TEXT,
    MODE_DETECT_SYSTEM_PROMPT,
    MODE_NAMES,
    SYSTEM_PROMPTS,
    _keyword_detect_mode,
    _parse_mode_prefix,
)
from .rpg_tools import RPGMixin, purge_group_rpg_data


def _content_to_text(content) -> str:
    """把存储的 content 还原成纯文本(给 .strip() / .split() / in / len() 用)。

    兼容 str / list of TextPart / list of dict / None / 任意类型。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                t = p.get("text", "")
                if t:
                    parts.append(t)
            elif hasattr(p, "text") and isinstance(p.text, str):
                parts.append(p.text)
        return "\n".join(parts)
    return str(content)


def _strip_xml_tags(text: str) -> str:
    """去除 <xxx>...</xxx> 标签块(包括内部内容),只保留用户真实输入。

    用于 /undo 预览时去掉 <system_reminder>、<Quoted Message>、<environment_details>
    等噪声。
    """
    import re

    cleaned = re.sub(
        r"<[A-Za-z_][\w\- ]*?>([\s\S]*?)</[A-Za-z_][\w\- ]*?>",
        "",
        text,
        flags=re.DOTALL,
    )
    cleaned = re.sub(r"</?[A-Za-z_][\w\- ]*?/?>", "", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _parse_docstring_params(docstring: str) -> dict:
    """从 @filter.llm_tool 风格的 docstring 抽取 parameters schema。

    复用 astrbot.core.star.register.star_handler 的解析思路:
    - 使用 docstring_parser 解析(正确处理多行参数描述)
    - 类型映射复用 astrbot.core.provider.func_tool_manager.PY_TO_JSON_TYPE

    格式:
        Args:
            param_name(type): desc(可换行续写)
            optional_param(type): desc(可换行续写) (description 含 "Optional"/"optional" → 可选)

    返回 OpenAI tool parameters 格式:
        {"type": "object", "properties": {name: {"type": ..., "description": ...}}, "required": [...]}
    """
    if not docstring:
        return {"type": "object", "properties": {}}

    parsed = docstring_parser.parse(docstring)
    properties: dict = {}
    required: list[str] = []

    for arg in parsed.params:
        type_name = (arg.type_name or "").strip()
        if not type_name:
            continue
        # 处理 list[type] / array[type] 这类嵌套
        sub_type_name = None
        nested = re.match(r"(\w+)\s*\[\s*(\w+)\s*\]", type_name)
        if nested:
            type_name, sub_type_name = nested.group(1), nested.group(2)
        json_type = PY_TO_JSON_TYPE.get(type_name.lower(), type_name.lower())
        prop: dict = {
            "type": json_type,
            "description": (arg.description or "").strip(),
        }
        if sub_type_name:
            sub_json_type = PY_TO_JSON_TYPE.get(
                sub_type_name.lower(), sub_type_name.lower()
            )
            if json_type == "array":
                prop["items"] = {"type": sub_json_type}
        properties[arg.arg_name] = prop
        # 必填判定:description 含 "optional" / "default" / "=" → 可选
        desc_lower = (arg.description or "").lower()
        if "optional" not in desc_lower and "default" not in desc_lower and "=" not in desc_lower:
            required.append(arg.arg_name)

    params: dict = {"type": "object", "properties": properties}
    if required:
        params["required"] = required
    return params


async def _extract_image(event: AstrMessageEvent) -> list[Image]:
    images: list[Image] = [
        comp for comp in event.get_messages() if isinstance(comp, Image)
    ]
    return images


def _build_quoted_tag(text: str):
    return f"<Quoted Message>\n{text}\n</Quoted Message>"


def _build_system_reminder(event: AstrMessageEvent) -> str:
    """构造系统提醒的 tag"""
    user_id = event.get_sender_id()
    user_nick = event.get_sender_name()

    return (
        f"<system_reminder>User ID: {user_id}, Nickname: {user_nick}</system_reminder>"
    )


class _LifeSimToolHooks(BaseAgentRunHooks[AstrAgentContext]):
    """从 run_context.messages 中提取本轮 agent 新增的工具调用上下文。

    工具调用 / 工具结果 / 思考 都保存在 run_context.messages 里,
    不在最终 LLMResponse 中(LLMResponse.tools_call_* 只剩最后一轮、且最终轮往往没有 tool call)。
    完整序列才能让下次 LLM 看到正确的对话历史。
    """

    def __init__(self) -> None:
        # tool_call_id → 最终 content(已经包含重复调用提示/超长落盘/follow-up)
        self.results_by_call_id: dict[str, str] = {}
        # 每一步:{content: [parts...], tool_calls: [{id, name, args}, ...]}
        self.steps: list[dict] = []
        self._before_count: int = 0

    async def on_agent_begin(self, run_context) -> None:
        self._before_count = len(run_context.messages)

    @staticmethod
    def _extract_tool_call(tc) -> dict | None:
        """从 ToolCall 对象或 dict 抽取 {id, name, args}。"""
        if hasattr(tc, "id"):
            tid = tc.id
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", None) if fn else None
            args_str = getattr(fn, "arguments", None) if fn else None
        elif isinstance(tc, dict):
            tid = tc.get("id")
            fn = tc.get("function") or {}
            name = fn.get("name") if isinstance(fn, dict) else None
            args_str = fn.get("arguments") if isinstance(fn, dict) else None
        else:
            return None
        if not tid or not name:
            return None
        if isinstance(args_str, dict):
            args = args_str
        else:
            try:
                args = json.loads(args_str) if args_str else {}
            except Exception:
                args = {}
        if not isinstance(args, dict):
            args = {}
        return {"id": tid, "name": name, "args": args}

    @staticmethod
    def _extract_content_parts(content) -> list:
        """从 AssistantMessageSegment.content(可能 list/str/None)抽取 ContentPart list。"""
        if content is None:
            return []
        if isinstance(content, str):
            return [TextPart(text=content)] if content else []
        if isinstance(content, list):
            return [c for c in content if c is not None]
        return []

    async def on_agent_done(self, run_context, llm_response) -> None:

        for msg in run_context.messages[self._before_count :]:
            role = getattr(msg, "role", None)
            if role == "tool":
                call_id = getattr(msg, "tool_call_id", None)
                content = getattr(msg, "content", None)
                if call_id and isinstance(content, str) and content:
                    self.results_by_call_id[call_id] = content
            elif role == "assistant":
                tcs = getattr(msg, "tool_calls", None) or []
                if not tcs:
                    continue
                step_calls = []
                for tc in tcs:
                    data = self._extract_tool_call(tc)
                    if data is not None:
                        step_calls.append(data)
                if not step_calls:
                    continue
                self.steps.append(
                    {
                        "content": self._extract_content_parts(
                            getattr(msg, "content", None)
                        ),
                        "tool_calls": step_calls,
                    }
                )


class LifeSimPlugin(DiceMixin, RPGMixin, Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir()
        self.kv_prefix = "life_sim_v1_"
        # AstrBot 在配置存在时传入,缺失时为 None
        self.config = config
        # 每个会话(group/user)一把 asyncio.Lock,防止同一会话并发触发 _generate 造成竞态
        self._sim_locks: dict[str, asyncio.Lock] = {}

    # ─── 配置读取助手 ────────────────────────────────────────

    def _cfg(self, key: str, default=None):
        """安全读 config(AstrBotConfig 继承自 dict,None 时返回 default)。"""
        try:
            if self.config is None:
                return default
            val = self.config.get(key, default)
            return val if val is not None else default
        except Exception:
            return default

    # ════════════════════════════════════════════════════════════════
    # 转生模拟:独立 KV 会话(叙事历史)
    # ════════════════════════════════════════════════════════════════

    def _sim_session_key(self, event: AstrMessageEvent) -> str:
        gid = event.message_obj.group_id
        if gid:
            return f"{self.kv_prefix}group_{gid}"
        return f"{self.kv_prefix}user_{event.get_sender_id()}"

    def _get_sim_lock(self, key: str) -> asyncio.Lock:
        """每个会话一把锁(惰性创建)。同一 key 上的并发命令直接返回处理中提示。"""
        lock = self._sim_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._sim_locks[key] = lock
        return lock

    async def _load_sim(self, event: AstrMessageEvent):
        key = self._sim_session_key(event)
        data = await self.get_kv_data(key, None)
        if data is None:
            return None
        if isinstance(data, dict):
            return data
        if isinstance(data, str):
            try:
                return json.loads(data)
            except Exception:
                return None
        return None

    async def _save_sim(self, event: AstrMessageEvent, session: dict):
        key = self._sim_session_key(event)
        logger.debug(f"life-sim: 保存会话到 KV({key}),消息={session}")
        try:
            await self.put_kv_data(key, session)
        except Exception:
            await self.put_kv_data(key, json.dumps(session, ensure_ascii=False))

    async def _clear_sim(self, event: AstrMessageEvent):
        key = self._sim_session_key(event)
        try:
            await self.delete_kv_data(key)
        except Exception as e:
            logger.warning(f"life-sim: 清除会话失败: {e}")

    def _extract_after_cmd(self, event: AstrMessageEvent, cmd: str) -> str:
        """提取 cmd 首次出现位置之后的所有内容。
        prefix 不再硬编码:AstrBot 的 @filter.command 会按系统配置识别 / ！ ~ 等
        (私聊可能无 prefix),找到 cmd 字符串的位置之后的全部就是参数。
        """
        text = (event.message_str or "").strip()
        if not text:
            return ""
        idx = text.find(cmd)
        if idx < 0:
            return ""
        return text[idx + len(cmd) :].strip()

    # ────────────────────────────────────────────────────────────────
    # 工具调用日志(hook 捕获 + 历史落盘)
    # ────────────────────────────────────────────────────────────────

    @staticmethod
    def _is_my_tool(name: str) -> bool:
        """过滤:只保留本插件的工具(rpg_*/roll_dice/life_sim_save_*)。"""
        return bool(name) and (
            name.startswith("rpg_")
            or name
            in {
                "roll_dice",
                "life_sim_save_character_lore",
                "life_sim_save_world_lore",
            }
        )

    def _build_my_tool_set(self) -> ToolSet:
        """直接从 self 自己的方法里收集本插件的工具,构建 ToolSet。

        不依赖 provider_manager.llm_tools(那是个间接层,会因版本/配置变化而不可用)。
        我们的工具就在 self 上(dir(self) 能拿到),匹配 rpg_*/roll_dice 名称即可。

        对每个匹配的 bound method,解析其 docstring 构造 parameters schema(让 LLM 知道
        怎么调用),再 new 一个 FunctionTool(handler=bound,parameters=...) 装入 ToolSet。
        用 bound method 作为 handler 避免 unbound 调用时 event 变 self 的 bug。

        缓存(运行时工具集不变)。
        """
        from astrbot.core.agent.tool import FunctionTool, ToolSet

        tool_set = ToolSet()

        for attr_name in dir(self):
            if not self._is_my_tool(attr_name):
                continue
            attr = getattr(self, attr_name, None)
            if attr is None or not callable(attr):
                continue
            # 已经是 FunctionTool 实例(装饰器有时会这样存)
            if hasattr(attr, "parameters") and hasattr(attr, "description"):
                tool_set.add_tool(attr)
                continue
            # 是 bound method — 自己包成 FunctionTool(补 schema + bound handler)
            doc = getattr(attr, "__doc__", None) or ""
            params = _parse_docstring_params(doc)
            new_tool = FunctionTool(
                name=attr_name,
                parameters=params,
                description=doc.split("\n\n")[0].strip() if doc else "",
                handler=attr,  # bound method,event 参数不会被当成 self
            )
            tool_set.add_tool(new_tool)

        if len(tool_set) == 0:
            logger.warning(
                "life-sim: 未找到任何 rpg_*/roll_dice 工具,请检查插件是否正确注册。"
            )

        web_search = self.context.provider_manager.llm_tools.get_func(
            "web_search_tavily"
        )
        tavily_extract_web_page = self.context.provider_manager.llm_tools.get_func(
            "tavily_extract_web_page"
        )
        if web_search and tavily_extract_web_page:
            tool_set.add_tool(web_search)
            tool_set.add_tool(tavily_extract_web_page)
        return tool_set

    async def _compress_history(self, messages: list, event=None) -> list:
        """压缩叙事历史:
        - 总长 ≤ max_history_chars 或 messages ≤ keep_tail_messages → 原样返回
        - 否则把前面的消息压缩为一段【叙事历史摘要】(优先 LLM,失败回退规则抽取),保留尾部 keep_tail 条原文
        下次压缩时,旧摘要会被纳入"前面",重新生成新摘要 — 摘要不会无限增长。
        """
        max_chars = int(self._cfg("max_history_chars", 60000))
        keep_tail = int(self._cfg("keep_tail_messages", 20))
        total = sum(len(_content_to_text(m.get("content"))) for m in messages)
        if total <= max_chars or len(messages) <= keep_tail:
            return messages

        head = messages[:-keep_tail]
        tail = list(messages[-keep_tail:])

        use_llm = bool(self._cfg("use_llm_compress", True))
        summary_text = None
        if use_llm:
            try:
                summary_text = await self._llm_summarize(head, event=event)
            except Exception as e:
                logger.warning(f"life-sim: LLM 压缩失败,回退规则摘要: {e}")
        if not summary_text:
            summary_text = self._build_history_summary(head, len(messages))

        # 兜底:摘要自身超长再截断
        if len(summary_text) > 8000:
            summary_text = summary_text[:8000] + "\n...(摘要进一步截断)"

        summary_msg = {"role": "user", "content": summary_text, "_summary": True}
        return [summary_msg] + tail

    async def _llm_detect_mode(self, world_setting: str, event=None) -> str:
        """用 LLM 分析世界观,返回模式 A/B/C。失败抛异常让上层回退关键词。

        Provider 优先级:mode_detect_provider_id > provider_id > 当前会话默认(若有 event)
        输出解析:仅取 response 中首个 A/B/C 字母;若没有则抛异常。
        """
        pid = str(self._cfg("mode_detect_provider_id", "") or "").strip()
        if not pid:
            pid = str(self._cfg("provider_id", "") or "").strip()
        if not pid and event is not None:
            pid, err = await self._get_provider_id(event, "A")
            if err:
                pid = None
        if not pid:
            raise RuntimeError(
                "无可用 provider (mode_detect_provider_id / provider_id / 会话默认都为空)"
            )

        user_msg = (
            f"世界观设定:\n---\n{world_setting[:3000]}\n---\n\n"
            f"请判断最适合的模式(只输出字母 A / B / C):"
        )

        llm_resp = await self.context.llm_generate(
            chat_provider_id=pid,
            system_prompt=MODE_DETECT_SYSTEM_PROMPT,
            contexts=[],
            prompt=user_msg,
        )
        text = (getattr(llm_resp, "completion_text", "") or "").strip().upper()
        for ch in text:
            if ch in "ABC":
                return ch
        raise RuntimeError(f"LLM 返回格式错误: {text[:80]}")

    async def _llm_summarize(self, head_msgs: list, event=None) -> str:
        """用 LLM 把头消息压缩成摘要。失败抛异常让上层回退到规则抽取。

        Provider 优先级:compress_provider_id > provider_id > 当前会话默认(若有 event)
        """
        from prompts import SUMMARY_SYSTEM_PROMPT

        pid = str(self._cfg("compress_provider_id", "") or "").strip()
        if not pid:
            pid = str(self._cfg("provider_id", "") or "").strip()
        if not pid and event is not None:
            pid, err = await self._get_provider_id(event, "A")
            if err:
                pid = None
        if not pid:
            raise RuntimeError(
                "无可用 provider (compress_provider_id / provider_id / 会话默认都为空)"
            )

        # 构造 contexts — 把 head_msgs 当上下文,要求模型摘要
        contexts = bind_checkpoint_messages(head_msgs)
        prompt = "请将上方历史对话压缩成简洁摘要,保留关键叙事线、人物关系、人生走向与结局标记。"

        llm_resp = await self.context.llm_generate(
            chat_provider_id=pid,
            system_prompt=SUMMARY_SYSTEM_PROMPT,
            contexts=contexts,
            prompt=prompt,
        )
        text = (getattr(llm_resp, "completion_text", "") or "").strip()
        if not text:
            raise RuntimeError("LLM 返回空摘要")
        return text

    def _build_history_summary(self, head_msgs: list, total_msgs: int) -> str:
        """从丢弃消息中结构性抽取摘要:世界观 + 主要阶段标题 + 结局标记。
        不调用 LLM,纯文本规则抽取 — 速度快、零成本。"""
        n_head = len(head_msgs)
        lines = [
            f"📜 [叙事历史摘要] 本局共 {total_msgs} 条对话,前 {n_head} 条已被压缩为以下要点。\n"
        ]

        # 世界观设定(找第一条非空 user 消息,通常就是 /创建 的输入)
        for m in head_msgs:
            if m.get("role") == "user":
                ws = _content_to_text(m.get("content")).strip()
                if ws and not ws.startswith("请"):
                    snippet = ws if len(ws) <= 500 else ws[:500] + "..."
                    lines.append(f"**世界观设定**:\n{snippet}\n")
                    break

        # 抽取 assistant 中的 ## 阶段标题作为"早期事件"列表(最多 40 条)
        titles = []
        for m in head_msgs:
            if m.get("role") == "assistant":
                for ln in _content_to_text(m.get("content")).split("\n"):
                    s = ln.strip()
                    if s.startswith("## "):
                        titles.append(s[3:].strip())
                        break
                    elif s.startswith("# "):
                        titles.append(s[2:].strip())
                        break
        if titles:
            lines.append(f"**早期主要事件**(按时间顺序,共 {len(titles)} 个):")
            for t in titles[:40]:
                lines.append(f"- {t}")
            if len(titles) > 40:
                lines.append(f"- 还有 {len(titles) - 40} 个早期事件省略")
            lines.append("")

        # 结局检测
        if any(
            m.get("role") == "assistant"
            and "<LIFE_SIM_END>" in _content_to_text(m.get("content"))
            for m in head_msgs
        ):
            lines.append(
                "⚠️ 早期曾达到人生结局(<LIFE_SIM_END>),后被用户选择继续/重新开局。"
            )

        # 早期用户决策(前 8 个非空 user 行动)
        user_actions = []
        for m in head_msgs:
            if m.get("role") == "user":
                a = _content_to_text(m.get("content")).strip()
                if a and not a.startswith("请") and len(a) <= 80:
                    user_actions.append(a)
        if user_actions:
            lines.append("\n**早期用户决策**: " + " | ".join(user_actions[:8]))

        summary = "\n".join(lines)
        # 摘要自身兜底,避免超长
        if len(summary) > 8000:
            summary = summary[:8000] + "\n...(摘要进一步截断)"
        return summary

    async def _get_provider_id(self, event: AstrMessageEvent, mode: str = "A"):
        """按 mode 选 provider 优先级:
        1. provider_mode_{a,b,c}(该模式专属配置)
        2. provider_id(全局主配置)
        3. 当前会话默认(get_current_chat_provider_id)
        """
        configured = (
            str(self._cfg(f"provider_mode_{mode.lower()}", "") or "").strip()
            or str(self._cfg("provider_id", "") or "").strip()
        )
        if configured:
            return configured, None

        umo = event.unified_msg_origin
        try:
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
        except Exception as e:
            return None, f"❌ 获取模型失败:{e}"
        if not provider_id:
            return None, "❌ 未配置聊天模型,请先在 WebUI 配置 LLM 提供商。"
        return provider_id, None

    # ════════════════════════════════════════════════════════════════
    # LLM 调用 — 按模式选择 llm_generate / tool_loop_agent
    # ════════════════════════════════════════════════════════════════

    async def _generate(
        self,
        event: AstrMessageEvent,
        session: dict,
        user_input: str,
        mode: str,
        imgs: list[Image] | None = None,
    ) -> str:
        # 同会话并发上锁:避免多次 LLM 调用/快照/save_sim 互相覆盖
        lock = self._get_sim_lock(self._sim_session_key(event))
        if lock.locked():
            return "⏳ 上一条消息还在处理中,请稍候再试..."

        async with lock:
            return await self._generate_locked(event, session, user_input, mode, imgs)

    async def _generate_locked(
        self,
        event: AstrMessageEvent,
        session: dict,
        user_input: str,
        mode: str,
        imgs: list[Image] | None,
    ) -> str:
        provider_id, err = await self._get_provider_id(event, mode)
        if err:
            return err

        system_prompt_tpl = SYSTEM_PROMPTS.get(mode, SYSTEM_PROMPTS["A"])
        # 提示词里没显式 [本局世界观] 占位符时,直接把设定拼到 system prompt 顶部
        if "[本局世界观]" in system_prompt_tpl:
            system_prompt = system_prompt_tpl.replace(
                "[本局世界观]", session.get("world_setting", "(未提供)")
            )
        else:
            system_prompt = (
                f"## 本局世界观\n---\n{session.get('world_setting', '(未提供)')}\n---\n\n"
                + system_prompt_tpl
            )

        # 注入持久化 lore(角色设定 + 世界观信息,直到 /删除 或 /创建)
        lore = self._build_lore_addendum(session)
        if lore:
            system_prompt += "\n\n" + lore

        messages = await self._compress_history(
            session.get("messages", []), event=event
        )

        # 用 turn 计数器快照 lore(单调递增,与消息位置/压缩解耦,稳定)
        turn = session.get("lore_turn", 0) + 1
        session["lore_turn"] = turn
        self._snapshot_lore(session, turn)
        # 同步快照 RPG 数值状态,供 /undo 回滚 HP/EXP/装备/会话等
        # mode B/C 一律保存(包括空快照)— 否则回滚到"首个创建 RPG 数据的 turn"时找不到快照,
        # 导致本应被删除的新建角色/会话漏网。
        if mode in ("B", "C"):
            rpg_snap = self._rpg_snapshot(event, mode)
            rpg_snaps = session.setdefault("rpg_snapshots", [])
            rpg_snaps.append({"turn": turn, **rpg_snap})
            # 限制最多保留 25 个快照(每个可能含多角色,避免 KV 膨胀)
            if len(rpg_snaps) > 25:
                del rpg_snaps[: len(rpg_snaps) - 25]

        contexts = bind_checkpoint_messages(messages)

        # 从 config 读工具调用参数(模式 B/C 用)
        tool_max_steps = max(1, min(100, int(self._cfg("tool_max_steps", 30))))
        tool_call_timeout = max(10, min(300, int(self._cfg("tool_call_timeout", 60))))

        system_reminder = _build_system_reminder(event)

        user_input += system_reminder

        image_urls = [(img.url or img.path) for img in imgs]
        tool_hooks: _LifeSimToolHooks | None = None
        try:
            if mode == "A":
                llm_resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    system_prompt=system_prompt,
                    image_urls=image_urls,
                    contexts=contexts,
                    prompt=user_input,
                )
            else:
                # 传 tools 让 LLM 知道 rpg_*/roll_dice 可用(否则 tool_loop_agent 不会调任何工具)
                tools = self._build_my_tool_set()
                tool_hooks = _LifeSimToolHooks()
                llm_resp = await self.context.tool_loop_agent(
                    event=event,
                    chat_provider_id=provider_id,
                    system_prompt=system_prompt,
                    image_urls=image_urls,
                    contexts=contexts,
                    prompt=user_input,
                    tools=tools,
                    max_steps=tool_max_steps,
                    tool_call_timeout=tool_call_timeout,
                    agent_hooks=tool_hooks,
                )
        except Exception as e:
            logger.error(f"life-sim: LLM 调用失败: {e}")
            return f"❌ 生成失败:{e}"

        # 拿到 final text(用于返回值 + 校验)
        text = (getattr(llm_resp, "completion_text", "") or "").strip()
        if not text:
            return "❌ 模型未返回内容,请重试。"

        # 把整轮(user + 工具调用 + 最终回应)一次性转成 AstrBot 原生 Message dict 列表
        new_msgs = await self._llm_resp_to_messages(
            user_input, llm_resp, imgs, tool_hooks
        )

        messages.extend(new_msgs)
        session["messages"] = messages
        await self._save_sim(event, session)
        return text

    async def _llm_resp_to_messages(
        self,
        user_input: str,
        llm_resp: LLMResponse,
        images: list[Image] | None,
        tool_hooks: "_LifeSimToolHooks | None" = None,
    ) -> list[dict]:
        """把一次 LLM 调用的结果直接转成 AstrBot 原生 Message dict 列表。

        单次调用可能产生 1~N 条 message:
        1. user input
        2. assistant with tool_calls(从 agent_hooks.tool_calls 抽,只保留本插件的工具)
        3. 每个 tool_call 一条 tool 消息(content = 真实返回值,通过 agent_hooks 捕获)
        4. 最终 assistant 文本

        直接 model_dump(),读取时 bind_checkpoint_messages 自动还原。
        """
        content: list[TextPart | ImageURLPart] = [TextPart(text=user_input)]

        if images:
            content += [
                ImageURLPart(
                    image_url=ImageURLPart.ImageURL(
                        url="data:image/png;base64," + await img.convert_to_base64()
                    )
                )
                for img in images
            ]

        msgs = [UserMessageSegment(content=content).model_dump()]

        # 从 agent_hooks 取本轮所有 step(覆盖多步调用;每步的 content 也保留下来)
        # 每步:AssistantMessageSegment(content + tool_calls) + N 个 ToolCallMessageSegment
        if tool_hooks is not None and tool_hooks.steps:
            for step in tool_hooks.steps:
                step_tool_calls: list[ToolCall] = []
                for tc in step["tool_calls"]:
                    if not self._is_my_tool(tc["name"]):
                        continue
                    try:
                        args_json = json.dumps(tc["args"], ensure_ascii=False)
                    except Exception:
                        args_json = "{}"
                    step_tool_calls.append(
                        ToolCall(
                            id=tc["id"],
                            function=ToolCall.FunctionBody(
                                name=tc["name"], arguments=args_json
                            ),
                        )
                    )
                if not step_tool_calls:
                    continue
                # content 可以是 [ThinkPart, TextPart] / str / None — 没有就 None
                step_content = step["content"] or None
                msgs.append(
                    AssistantMessageSegment(
                        content=step_content,
                        tool_calls=step_tool_calls,
                    ).model_dump()
                )
                for tc in step_tool_calls:
                    # 没拿到结果时用空串兜底(None 会让 ToolCallMessageSegment 校验失败)
                    real_content = tool_hooks.results_by_call_id.get(tc.id) or ""
                    msgs.append(
                        ToolCallMessageSegment(
                            content=real_content,
                            tool_call_id=tc.id,
                        ).model_dump()
                    )
        if llm_resp.result_chain is not None and llm_resp.result_chain.chain:
            final_content = llm_resp.result_chain.chain
        else:
            # tool_loop_agent 最终响应有时 result_chain 为 None;
            # 兜底从 _completion_text + reasoning_content 重建(保留 thinking)
            text = (getattr(llm_resp, "_completion_text", "") or "").strip()
            think = (getattr(llm_resp, "reasoning_content", None) or "").strip()
            think_sig = getattr(llm_resp, "reasoning_signature", None)
            final_content = []
            if think:
                final_content.append(ThinkPart(think=think, encrypted=think_sig))
            if text:
                final_content.append(TextPart(text=text))
            if not final_content:
                final_content = [TextPart(text="(模型未输出文本)")]
        msgs.append(AssistantMessageSegment(content=final_content).model_dump())
        logger.debug(f"life-sim resp: {msgs[-1]}")
        return msgs

    # ════════════════════════════════════════════════════════════════
    # 持久化 lore(角色设定 + 世界观,直到 /删除 或 /创建)
    # ════════════════════════════════════════════════════════════════

    async def _save_lore(self, event, key: str, section: str, content: str) -> str:
        """保存到 session.world_lore / session.character_lore(同 section 覆盖)。"""
        session = await self._load_sim(event)
        if not session:
            return "❌ 当前没有活动会话,请先 /创建"
        lore_list = session.get(key) or []
        lore_list = [e for e in lore_list if e.get("section") != section]
        lore_list.append(
            {
                "section": section,
                "content": content,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        session[key] = lore_list
        await self._save_sim(event, session)
        return f"✅ 「{section}」已保存({len(content)}字)"

    def _snapshot_lore(self, session: dict, turn: int):
        """在 turn 处快照当前 lore 状态,供 /undo 回滚。

        每个 turn 开始时(LLM 调用前)调用一次。/undo 时用 turn 计数回滚,
        比 msg_index 更稳定 — 压缩 / 增删消息不影响 turn 计数。

        深拷贝(每条 entry 复制 dict)避免后续修改 session.lore 影响快照。
        """
        snapshots = session.setdefault("lore_snapshots", [])
        snapshots.append(
            {
                "turn": turn,
                "world_lore": [dict(e) for e in (session.get("world_lore") or [])],
                "character_lore": [
                    dict(e) for e in (session.get("character_lore") or [])
                ],
            }
        )

    def _build_lore_addendum(self, session: dict) -> str:
        """构造注入到 system prompt 的 lore 附加段。"""
        parts = []
        world_lore = session.get("world_lore") or []
        if world_lore:
            lines = ["## 持久化世界观(用户在对话中确认过的设定,自动注入每次对话)"]
            for e in world_lore:
                lines.append(f"### {e['section']}\n{e['content']}")
            parts.append("\n".join(lines))
        char_lore = session.get("character_lore") or []
        if char_lore:
            lines = ["## 持久化角色设定(用户在对话中确认过的设定,自动注入每次对话)"]
            for e in char_lore:
                lines.append(f"### {e['section']}\n{e['content']}")
            parts.append("\n".join(lines))
        return "\n\n".join(parts)

    @filter.llm_tool(name="life_sim_save_world_lore")
    async def life_sim_save_world_lore(
        self, event, content: str, section: str = "general"
    ) -> str:
        """
        永久保存世界观信息

        适用场景:
        - 世界规则(魔法体系 / 科技水平 / 宗教等)
        - 政治格局 / 势力分布 / 重要国家或组织
        - 地理 / 历史背景 / 重要事件
        - 已确认的重要 NPC 设定

        Args:
            content(string): 世界观内容(详细描述,一段或多段)
            section(string): 分类标签,如 "魔法体系"、"主要势力"、"地理"。默认 "general"。同 section 会被覆盖,不同 section 累积保存。
        Returns:
            确认消息。
        """
        return await self._save_lore(event, "world_lore", section, content)

    @filter.llm_tool(name="life_sim_save_character_lore")
    async def life_sim_save_character_lore(
        self, event, content: str, section: str = "general"
    ) -> str:
        """
        永久保存角色设定

        适用场景:
        - 形态变化(变身 / 进化 / 解锁新形态 / 退化)
        - 外貌变化(受伤 / 服装 / 装饰 / 年龄增长)
        - 性格变化(觉醒 / 黑化 / 成长 / 信念改变)
        - 重要记忆 / 关系变化
        - 习得技能 / 称号 / 职业变更

        Args:
            content(string): 角色设定内容(详细描述)
            section(string): 分类标签,如 "forms"、"appearance"、"personality"、"relationships"、"skills"。默认 "general"。同 section 会被覆盖。
        Returns:
            确认消息。
        """
        return await self._save_lore(event, "character_lore", section, content)

    # ════════════════════════════════════════════════════════════════
    # 指令
    # ════════════════════════════════════════════════════════════════

    @filter.command("测试")
    async def cmd_test(self, event: AstrMessageEvent):
        """/测试 - 测试插件是否可用"""
        imgs = await _extract_image(event)
        yield event.plain_result("data: " + json.dumps(imgs))

    @filter.command("创建")
    async def cmd_create(self, event: AstrMessageEvent):
        """/创建 [rpg|dnd] <世界观设定> - 创建转生模拟会话(覆盖已有)"""
        setting = self._extract_after_cmd(event, "创建")
        extractor = QuotedMessageExtractor(event=event)
        quoted = await extractor.text()
        if quoted:
            setting += "\n" + _build_quoted_tag(quoted)
        if not setting:
            yield event.plain_result(HELP_TEXT)
            return

        imgs = await _extract_image(event)

        # 显式前缀 > 自动识别
        prefix_mode, cleaned = _parse_mode_prefix(setting)
        if prefix_mode:
            mode = prefix_mode
            setting = cleaned
        else:
            if self._cfg("use_llm_mode_detect", True):
                try:
                    mode = await self._llm_detect_mode(setting, event=event)
                    logger.info(f"life-sim: LLM 模式识别={mode}")
                except Exception as e:
                    logger.warning(f"life-sim: LLM 模式识别失败,回退关键词: {e}")
                    mode = _keyword_detect_mode(setting)
            else:
                mode = _keyword_detect_mode(setting)

        await self._clear_sim(event)

        session = {
            "world_setting": setting,
            "mode": mode,
            "owner_id": event.get_sender_id(),
            "owner_name": event.get_sender_name(),
            "created_at": event.message_obj.timestamp,
            "messages": [],
        }
        await self._save_sim(event, session)

        yield event.plain_result(
            f"🎬 命运开始转动 [模式 {mode} - {MODE_NAMES[mode]}],正在编织你的人生..."
        )

        if mode == "C":
            startup_steps = (
                "1) 叙事前先调 rpg_create_session,game_system 必须为 dnd5e\n"
                "2) 再调 rpg_join_session 建角色;用户未明确给六维时将 ability_scores 留空,"
                "由工具自动掷 6 次 4d6kh3 并持久化\n"
                "3) 确认工具角色卡完整显示 STR/DEX/CON/INT/WIS/CHA 后,再输出角色卡和开场叙事\n"
            )
        elif mode == "B":
            startup_steps = (
                "1) 如需战斗/数值管理,先调 rpg_create_session 建会话再 rpg_join_session 建角色\n"
                "2) 然后输出角色卡并开始开场叙事\n"
            )
        else:
            startup_steps = (
                "1) 先输出角色卡(姓名/性别/出生地/天赋/家庭)\n"
                "2) 然后从婴幼儿期开始第一段叙事(## 0岁 这样的标题)\n"
            )

        first_input = (
            f"世界观设定:{setting}\n\n"
            # "请直接开始(不要再追问细节):\n"
            + startup_steps
            + "最后,这一轮**不要**给出人生总结,故事需要用户多次推进"
        )
        result = await self._generate(event, session, first_input, mode, imgs)
        yield event.plain_result(result)

    @filter.command("do", alias={"input", "输入"})
    async def cmd_input(self, event: AstrMessageEvent):
        """/do <选项/自定义行动/反馈> - 继续推进模拟"""
        action = self._extract_after_cmd(event, "do")
        extractor = QuotedMessageExtractor(event=event)
        quoted = await extractor.text()
        if quoted:
            action += "\n" + _build_quoted_tag(quoted)
        if not action:
            action = "请继续推进剧情(没有任何特定选择,按既定轨迹自然发展)"

        imgs = await _extract_image(event)

        session = await self._load_sim(event)
        if not session:
            yield event.plain_result(
                "❌ 当前没有进行中的转生模拟。\n请先使用 /创建 <世界观> 开始。"
            )
            return

        mode = session.get("mode", "A")
        messages = session.get("messages", [])
        last_asst = ""
        for m in reversed(messages):
            if m.get("role") == "assistant":
                last_asst = _content_to_text(m.get("content"))
                break
        if "<LIFE_SIM_END>" in last_asst:
            yield event.plain_result(
                "🎬 这段人生已经结束啦!\n"
                "使用 /删除 清除旧会话,或 /创建 开始一段新的人生。"
            )
            return

        yield event.plain_result(f"⏳ 命运的齿轮转动中... [模式 {mode}]")
        result = await self._generate(event, session, action, mode, imgs)
        yield event.plain_result(result)

    @filter.command("进度")
    async def cmd_progress(self, event: AstrMessageEvent):
        """/进度 - 查看当前模拟进度"""
        session = await self._load_sim(event)
        if not session:
            yield event.plain_result(
                "❌ 当前没有进行中的转生模拟,请先使用 /创建 <世界观> 开始。"
            )
            return

        messages = session.get("messages", [])
        world = session.get("world_setting", "")
        owner = session.get("owner_name", "")
        mode = session.get("mode", "A")
        asst_msgs = [m for m in messages if m.get("role") == "assistant"]
        turn_count = len(asst_msgs)

        lines = [f"📜 转生模拟进度  |  模式: {mode} - {MODE_NAMES[mode]}"]
        if owner:
            lines.append(f"👤 创建玩家:{owner}")
        world_disp = world if len(world) <= 120 else world[:120] + "..."
        lines.append(f"🌍 世界:{world_disp}")
        lines.append(f"🔄 已交互:{turn_count} 轮")

        if asst_msgs:
            last = _content_to_text(asst_msgs[-1].get("content"))
            title = ""
            for line in last.split("\n"):
                s = line.strip()
                if s.startswith("## "):
                    title = s[3:].strip()
                    break
                elif s.startswith("# "):
                    title = s[2:].strip()
                    break
            if title:
                lines.append(f"📍 当前位置:{title}")
            if "<LIFE_SIM_END>" in last:
                lines.append("🏁 人生已结束")

            tail = last if len(last) <= 800 else "..." + last[-800:]
            lines.append(f"\n—— 最近一段 ——\n{tail}")

        yield event.plain_result("\n".join(lines))

    @filter.command("dump")
    async def cmd_dump(self, event: AstrMessageEvent):
        """/dump [full] - 调试用,把当前会话从 KV 完整导出为 JSON。
        不带参数:只导出 messages + 摘要字段;
        full:导出整个 session(包含 lore/快照等)。"""
        arg = self._extract_after_cmd(event, "dump").strip().lower()
        full = arg in ("full", "all", "完整", "全部")

        session = await self._load_sim(event)
        if not session:
            yield event.plain_result("❌ 当前没有活动会话,请先 /创建")
            return

        if full:
            payload = session
        else:
            payload = {
                "mode": session.get("mode"),
                "world_setting": session.get("world_setting", ""),
                "owner_id": session.get("owner_id"),
                "owner_name": session.get("owner_name"),
                "created_at": session.get("created_at"),
                "lore_turn": session.get("lore_turn"),
                "messages": session.get("messages", []),
                "message_count": len(session.get("messages", [])),
            }

        try:
            text = json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception as e:
            yield event.plain_result(f"❌ 序列化失败:{e}")
            return

        header = (
            f"📦 session dump ({'full' if full else 'summary'})"
            f"  key=`{self._sim_session_key(event)}`"
        )
        if len(text) > 3000:
            logger.info(text)
            event.stop_event()
            return
        # 头部 + 内容,避免太长看不到 key
        yield event.plain_result(f"{header}\n{text}")

    @filter.command("删除")
    async def cmd_delete(self, event: AstrMessageEvent):
        """/删除 - 删除当前会话(同时清理对应 RPG 存档)"""
        session = await self._load_sim(event)
        if not session:
            yield event.plain_result("❌ 当前没有进行中的转生模拟。")
            return

        group_id = self._get_group_id(event)
        try:
            purge = purge_group_rpg_data(self.data_dir, group_id)
        except Exception as e:
            logger.debug(f"life-sim: 清理 RPG 存档失败: {e}")
            purge = {"deleted_chars": 0, "deleted_sessions": []}

        await self._clear_sim(event)
        char_note = (
            f",{purge['deleted_chars']} 个 RPG 存档" if purge["deleted_chars"] else ""
        )
        sess_note = (
            f",{len(purge['deleted_sessions'])} 个 RPG 会话文件"
            if purge["deleted_sessions"]
            else ""
        )
        yield event.plain_result(
            "🗑️ 会话已删除"
            f"{char_note}{sess_note}。\n"
            "使用 /创建 <世界观> 可以开始一段新的人生。"
        )

    @filter.command("undo")
    async def cmd_undo(self, event: AstrMessageEvent):
        """/undo [N] - 撤销最近 N 轮对话(默认 1)。叙事历史、持久化 lore、RPG 数值(HP/EXP/装备/会话)全部回滚"""
        arg = self._extract_after_cmd(event, "undo").strip()
        n = 1
        if arg:
            try:
                n = int(arg)
            except ValueError:
                yield event.plain_result("❌ 用法:`/undo [N]`,N 为 1-20 的整数")
                return
            if n < 1 or n > 20:
                yield event.plain_result("❌ N 必须在 1-20 之间")
                return

        session = await self._load_sim(event)
        if not session:
            yield event.plain_result("❌ 当前没有活动会话")
            return

        messages = session.get("messages", [])
        # 找最近 N 个 user 消息的索引
        user_indices = [i for i, m in enumerate(messages) if m.get("role") == "user"]
        if not user_indices:
            yield event.plain_result("❌ 没有可撤销的轮次")
            return

        # 取最近的 N 个 user 位置作为截断点
        # user_indices 严格按 enumerate 顺序(时间升序),[-take] = 倒数第 N 个 = 第 N 新的 user
        take = min(n, len(user_indices))
        cut_idx = user_indices[-take]

        # 截断
        removed = messages[cut_idx:]
        messages = messages[:cut_idx]

        # 回滚持久化 lore:用 turn 计数,不受压缩影响
        current_turn = session.get("lore_turn", 0)
        # target_turn = 当前 turn - take + 1 = 第一个被回滚的 turn;
        # 该 turn 的快照 = "该 turn 尚未执行任何工具调用"的状态,正好是我们要恢复到的状态。
        # max(1, ...) 防止 target_turn=0 时找不到快照(从 turn=1 开始计数)。
        target_turn = max(1, current_turn - take + 1)
        snapshots = session.get("lore_snapshots") or []
        target_snapshot = next(
            (s for s in reversed(snapshots) if s["turn"] == target_turn),
            None,
        )
        if target_snapshot:
            session["world_lore"] = target_snapshot["world_lore"]
            session["character_lore"] = target_snapshot["character_lore"]
        # 删掉被回滚的快照(turn > target_turn)
        session["lore_snapshots"] = [s for s in snapshots if s["turn"] <= target_turn]
        # 同时回滚 lore_turn 计数
        session["lore_turn"] = target_turn
        lore_restored = target_snapshot is not None

        # 回滚 RPG 数值状态(HP/EXP/装备/会话/角色档案)
        rpg_snapshots = session.get("rpg_snapshots") or []
        target_rpg_snap = next(
            (s for s in reversed(rpg_snapshots) if s["turn"] == target_turn),
            None,
        )
        rpg_stats = None
        if target_rpg_snap is not None:
            rpg_stats = self._rpg_restore(target_rpg_snap)
        session["rpg_snapshots"] = [
            s for s in rpg_snapshots if s["turn"] <= target_turn
        ]

        session["messages"] = messages
        await self._save_sim(event, session)

        # 统计
        user_n = sum(1 for m in removed if m.get("role") == "user")
        asst_n = sum(1 for m in removed if m.get("role") == "assistant")
        tool_n = sum(1 for m in removed if m.get("role") == "tool")
        summary_n = sum(1 for m in removed if m.get("_summary"))
        lines = [
            f"⏪ 已撤销最近 {user_n} 轮对话(删 {len(removed)} 条消息)",
            f"   组成:user × {user_n}, assistant × {asst_n}, tool × {tool_n}"
            + (f", summary × {summary_n}" if summary_n else ""),
            f"   剩余历史 {len(messages)} 条",
        ]
        if lore_restored:
            target_snap = next(
                (s for s in reversed(snapshots) if s["turn"] == target_turn), None
            )
            if target_snap is not None:
                w_n = len(target_snap["world_lore"])
                c_n = len(target_snap["character_lore"])
                if w_n or c_n:
                    parts = []
                    if w_n:
                        parts.append(f"世界观 {w_n} 条")
                    if c_n:
                        parts.append(f"角色 {c_n} 条")
                    lines.append(f"   📜 持久化设定回滚:{' + '.join(parts)}")
                else:
                    lines.append("   📜 持久化设定回滚(本次 turn 无 lore 变更)")
            else:
                lines.append("   📜 持久化设定回滚")
        if rpg_stats is not None:
            rc = rpg_stats["restored_chars"]
            rs = rpg_stats["restored_sessions"]
            dc = rpg_stats["deleted_chars"]
            ds = rpg_stats["deleted_sessions"]
            if rc or rs or dc or ds:
                parts = []
                if rc:
                    parts.append(f"恢复角色 ×{rc}")
                if rs:
                    parts.append(f"恢复会话 ×{rs}")
                if dc:
                    parts.append(f"删除角色 ×{dc}")
                if ds:
                    parts.append(f"删除会话 ×{ds}")
                lines.append(f"   🎮 RPG 数值已回滚:{', '.join(parts)}")
            else:
                lines.append("   🎮 RPG 数值已回滚(无变化)")
        elif session.get("mode") in ("B", "C"):
            lines.append("   ⚠️ 未找到该 turn 的 RPG 快照(数值未回滚),用 /删除 重建会话")
        # 预览被撤销的最后一个 user 输入(去掉 <system_reminder>、<Quoted Message> 等标签)
        last_user = next(
            (m for m in reversed(removed) if m.get("role") == "user"), None
        )
        if last_user:
            raw = _content_to_text(last_user.get("content"))
            stripped = _strip_xml_tags(raw)
            preview = stripped[:60]
            more = "..." if len(stripped) > 60 else ""
            lines.append(f"   撤销的最后输入:`{preview}{more}`")
        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        logger.info("life-sim: 插件已卸载")
