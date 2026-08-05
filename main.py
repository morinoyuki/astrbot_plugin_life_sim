"""转生模拟器 AstrBot 插件 - 主入口
- 模式 A: 纯叙事(默认)
- 模式 B: 游戏世界 RPG(HP/等级/装备/经验) — 来自 rpg_tools.RPGMixin
- 模式 C: DND 跑团(RPG + D20 骰子) — 来自 dice.DiceMixin
- 独立上下文: 叙事历史 KV 存储 + 显式 contexts
- 4 个指令: /创建 /do /进度 /删除
"""

import asyncio
import json
import os
import re
import time

import docstring_parser
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.message import (
    AssistantMessageSegment,
    ImageURLPart,
    TextPart,
    ThinkPart,
    ToolCall,
    ToolCallMessageSegment,
    UserMessageSegment,
    bind_checkpoint_messages,
)
from astrbot.core.agent.tool import ToolSet
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Image
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.provider.func_tool_manager import PY_TO_JSON_TYPE
from astrbot.core.star.star_tools import StarTools
from astrbot.core.utils.quoted_message.extractor import QuotedMessageExtractor

from .dice import DiceMixin
from .prompts import (
    HELP_TEXT,
    MODE_DETECT_SYSTEM_PROMPT,
    MODE_NAMES,
    SUMMARY_SYSTEM_PROMPT,
    SYSTEM_PROMPTS,
    _keyword_detect_mode,
    _parse_mode_prefix,
)
from .rpg_tools import RPGMixin
from .storage_narrative import NarrativeStore
from .storage_rpg import RpgStore
from .storage_sim import SimStore


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


def _parse_tool_from_docstring(docstring: str) -> tuple[str, dict]:
    """从 llm_tool 风格的 docstring 一次解析出 (description, parameters schema)。

    复用 astrbot.core.star.register.star_handler 的解析思路:
    - 使用 docstring_parser 解析(正确处理多行描述、含 / 不含空行分隔符)
    - 类型映射复用 astrbot.core.provider.func_tool_manager.PY_TO_JSON_TYPE

    docstring 格式:
        <description: 多行 summary,可含 bullet 列表,直到 Args:/Returns:/... 之前>
        Args:
            param_name(type): desc(可换行续写)
            optional_param(type): desc(可换行续写) (description 含 "Optional"/"optional" → 可选)

    返回:
        description: 短描述(str,可能含换行)
        parameters: OpenAI tool parameters 格式
            {"type": "object", "properties": {name: {"type": ..., "description": ...}}, "required": [...]}
    """
    if not docstring:
        return "", {"type": "object", "properties": {}}

    parsed = docstring_parser.parse(docstring)
    description = (parsed.description or "").strip()

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
        if (
            "optional" not in desc_lower
            and "default" not in desc_lower
            and "=" not in desc_lower
        ):
            required.append(arg.arg_name)

    parameters: dict = {"type": "object", "properties": properties}
    if required:
        parameters["required"] = required
    return description, parameters


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
            except (json.JSONDecodeError, TypeError):
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
        # 文件存储实例(sim 会话 + RPG 数据 + 剧情历史,各自独立模块)
        self.sim_store = SimStore(self.data_dir)
        self.rpg_store = RpgStore(self.data_dir)
        self.narrative_store = NarrativeStore(self.data_dir)
        # AstrBot 在配置存在时传入,缺失时为 None
        self.config = config
        # 每个会话(group/user)一把 asyncio.Lock,防止同一会话并发触发 _generate 造成竞态
        self._sim_locks: dict[str, asyncio.Lock] = {}
        # 工具调用期间的 lore 暂存:{event_key: {"world_lore": [...], "character_lore": {...}}}
        # 工具 handler 只写这里,_generate 结束时统一合并到 session 并落库,
        # 避免工具内 _load_sim 拿到新 dict B 后又被外层旧 dict A 全量覆写。
        self._pending_lore: dict[str, dict] = {}
        # 本轮 revise 标记:{event_key: bool} — 本轮 LLM 是否调用过
        # life_sim_revise_narrative?若是,跳过本轮的 _auto_record_narrative
        # (避免修订后的剧情被同时当成"新记录"再存一份)
        self._pending_revise: dict[str, bool] = {}

    # ─── 配置读取助手 ────────────────────────────────────────

    def _cfg(self, key: str, default=None):
        """安全读 config(AstrBotConfig 继承自 dict,None 时返回 default)。"""
        if self.config is None:
            return default
        try:
            val = self.config.get(key, default)
        except (AttributeError, TypeError):
            return default
        return val if val is not None else default

    # ════════════════════════════════════════════════════════════════
    # 转生模拟:独立文件会话(叙事历史)
    # ════════════════════════════════════════════════════════════════

    def _sim_session_key(self, event: AstrMessageEvent) -> str:
        gid = event.message_obj.group_id
        if gid:
            return f"group_{gid}"
        return f"user_{event.get_sender_id()}"

    def _get_sim_lock(self, key: str) -> asyncio.Lock:
        """每个会话一把锁(惰性创建)。同一 key 上的并发命令直接返回处理中提示。"""
        lock = self._sim_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._sim_locks[key] = lock
        return lock

    async def _load_sim(self, event: AstrMessageEvent):
        return await self.sim_store.load(self._sim_session_key(event))

    async def _save_sim(self, event: AstrMessageEvent, session: dict):
        await self.sim_store.save(self._sim_session_key(event), session)

    async def _clear_sim(self, event: AstrMessageEvent):
        await self.sim_store.delete(self._sim_session_key(event))

    def _busy_message(self) -> str:
        return "⏳ 上一条消息还在处理中,请稍候再试..."

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
        """过滤:只保留本插件的工具(rpg_*/roll_dice/life_sim_save_*/life_sim_revise_narrative)。"""
        return bool(name) and (
            name.startswith("rpg_")
            or name
            in {
                "roll_dice",
                "life_sim_save_character_lore",
                "life_sim_save_world_lore",
                "life_sim_revise_narrative",
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
            # 是 bound method — 自己包成 FunctionTool(补 schema + bound handler)
            doc = getattr(attr, "__doc__", None) or ""
            description, parameters = _parse_tool_from_docstring(doc)
            new_tool = FunctionTool(
                name=attr_name,
                parameters=parameters,
                description=description,
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
            except (ValueError, KeyError, TimeoutError, OSError, ConnectionError) as e:
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
        except (KeyError, ValueError, LookupError) as e:
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
        imgs: list[Image] | None,
    ) -> str:
        provider_id, err = await self._get_provider_id(event, mode)
        if err:
            return err

        # 为本轮开一个 staging 槽位:工具 handler 写到 self._pending_lore[event_key],
        # 本函数末尾统一合并到 session 并落库(成功路径)。失败路径在 finally 释放。
        event_key = self._sim_session_key(event)
        self._pending_lore[event_key] = {}
        self._pending_revise[event_key] = False

        world_setting = session.get("world_setting")
        system_prompt_tpl = SYSTEM_PROMPTS.get(mode, SYSTEM_PROMPTS["A"])
        # 完整设定作为独立段落追加在 system prompt 末尾。
        system_prompt = system_prompt_tpl
        if world_setting:
            system_prompt += (
                f"\n\n## 本局世界观(全文,{len(world_setting)} 字)\n\n{world_setting}\n"
            )

        # 注入持久化 lore(角色设定 + 世界观信息,直到 /删除 或 /创建)
        lore = self._build_lore_addendum(session)
        if lore:
            system_prompt += "\n\n" + lore

        # 注入 last_narrative_id — 让 LLM 知道如何调用 life_sim_revise_narrative
        last_nid = session.get("last_narrative_id")
        if last_nid:
            system_prompt += (
                f"\n\n## 📌 最近剧情ID\n"
                f"`{last_nid}` — 这是你**上一段输出**对应的剧情记录 ID。\n"
                f"用户反馈那段剧情需要修改时,直接调\n"
                f"`life_sim_revise_narrative(record_id=\"{last_nid}\", narrative=\"<新剧情全文>\")`\n"
                f"即可覆盖,不必让用户复制 ID。\n"
                f"(也可以省略 record_id,会自动修订最近一条。)"
            )

        # 输出前自检 — 放在 system prompt 最末尾,利用 recency bias 强化设定遵从度
        system_prompt += (
            "\n\n## ✅ 输出前自检清单(写正文前必须过一遍)\n"
            "1. **本次要描写的角色是否已在「持久化角色设定」中?** — 在的话,先把他的 `appearance` / `forms` / `personality` 字段值在脑子里过一遍\n"
            "2. **发色 / 瞳色 / 发型 / 服装 / 配饰**是否与设定一致?不一致就改;不要拿「氛围需要」「光线效果」「换季了」当借口\n"
            "3. **外貌以外的世界观细节**(地点名、组织名、规则)是否与「持久化世界观」一致?\n"
            "4. **本次要记录的新事实**是否已经在设定里?是的话不要重复调 `life_sim_save_*_lore`\n"
            "任何与持久化设定冲突的描写 = 违规,即使「写得更好看」也不允许。"
        )

        messages = await self._compress_history(
            session.get("messages", []), event=event
        )

        # 用 turn 计数器快照 lore(单调递增,与消息位置/压缩解耦,稳定)
        turn = session.get("lore_turn", 0) + 1
        session["lore_turn"] = turn
        self._snapshot_lore(session, turn)
        # 同步快照剧情历史状态(供 /undo 回滚被本 turn 新增/修订的记录)
        # 必须在 LLM 调用前抓取 — `_auto_record_narrative` 在调用结束后才写。
        await self._snapshot_narrative_history(session, turn, event_key)
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
        except (ValueError, KeyError, TimeoutError, OSError, ConnectionError) as e:
            logger.error(f"life-sim: LLM 调用失败: {e}")
            self._pending_lore.pop(event_key, None)
            self._pending_revise.pop(event_key, None)
            return f"❌ 生成失败:{e}"

        # 拿到 final text(用于返回值 + 校验)
        text = (getattr(llm_resp, "completion_text", "") or "").strip()
        if not text:
            self._pending_lore.pop(event_key, None)
            self._pending_revise.pop(event_key, None)
            return "❌ 模型未返回内容,请重试。"

        # 把整轮(user + 工具调用 + 最终回应)一次性转成 AstrBot 原生 Message dict 列表
        new_msgs = await self._llm_resp_to_messages(
            user_input, llm_resp, imgs, tool_hooks
        )

        messages.extend(new_msgs)
        session["messages"] = messages

        # 把本轮工具暂存的 lore 合并到 session,随消息一起落库。
        # 这是唯一一次的 _save_sim,工具 handler 内部不再写 KV。
        staging = self._pending_lore.pop(event_key, {}) or {}
        for k in ("world_lore", "character_lore"):
            if k in staging:
                session[k] = staging[k]

        # 本轮是否调用过 life_sim_revise_narrative?若是,跳过 auto_record —
        # revise 已经把正确内容写回老记录,本轮的 text 响应是修订后的副本,
        # 再记一遍会出现内容几乎相同的重复记录。
        revise_called = self._pending_revise.pop(event_key, False)

        await self._save_sim(event, session)

        # ─── 自动记录剧情历史(独立存储,与 sim session 解耦)───
        # 若本轮已调用 revise,跳过 — 老记录已被覆盖,无需再起新记录。
        if revise_called:
            record_id = None
        else:
            record_id = await self._auto_record_narrative(
                session=session,
                scope=event_key,
                user_action=user_input,
                narrative=text,
            )
        if record_id:
            session["last_narrative_id"] = record_id
            await self._save_sim(event, session)
            text += f"\n\n📝 [剧情ID: `{record_id}`]"

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
                    except OSError:
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

    async def _save_lore(
        self,
        event,
        key: str,
        section: str,
        content: str,
        character: str | None = None,
    ) -> str:
        """**追加**一条 lore 到 self._pending_lore,**永不覆盖**(系统按时间线保留)。

        world_lore:list 结构 [{seq, section, content, updated_at}]
        character_lore:dict 结构 {角色名: [{seq, section, content, updated_at}]}

        每次调用都生成新的 seq(基于 staging 里已有最大 seq + 1),保证
        老细节不会被新调用冲掉,即使 LLM 忘记"先读再写"也不会丢东西。

        调用前提:`_generate` 已在 `self._pending_lore[event_key]` 上初始化了一个空 dict
        (tool handler 永远在 `_generate` 持有的 `tool_loop_agent` 流程内被调)。
        """
        event_key = self._sim_session_key(event)
        staging = self._pending_lore[event_key]

        # 首次写该 key 时从磁盘拉基线,让本 turn 的多次 save 都能看到之前 turn 的 lore
        if key not in staging:
            baseline = await self._load_sim(event)
            if baseline:
                existing = baseline.get(key)
                if key == "character_lore":
                    staging[key] = self._normalize_character_lore(existing)
                else:
                    staging[key] = [dict(e) for e in (existing or [])]
            else:
                staging[key] = (
                    self._normalize_character_lore(None)
                    if key == "character_lore"
                    else []
                )

        seq = self._next_lore_seq(staging, key, character)
        self._append_lore_entry(staging, key, section, content, character, seq)
        label = self._lore_label(key, section, character)
        return f"✅ 「{label}」已暂存 [#{seq}]({len(content)}字,本轮结束时统一落库)"

    @staticmethod
    def _append_lore_entry(
        target: dict,
        key: str,
        section: str,
        content: str,
        character: str | None,
        seq: int,
    ):
        """追加单条 lore(就地修改 target[key])。**永不覆盖已有条目**。"""
        entry = {
            "seq": seq,
            "section": section,
            "content": content,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if key == "character_lore":
            char_name = (character or "主角").strip() or "主角"
            lore_dict = LifeSimPlugin._normalize_character_lore(
                target.get("character_lore")
            )
            char_list = list(lore_dict.get(char_name, []))
            char_list.append(entry)
            lore_dict[char_name] = char_list
            target["character_lore"] = lore_dict
        else:
            lore_list = list(target.get(key) or [])
            lore_list.append(entry)
            target[key] = lore_list

    @staticmethod
    def _next_lore_seq(target: dict, key: str, character: str | None) -> int:
        """从 target[key] 里取已有 seq 的最大值 +1;无则从 1 开始。"""
        if key == "character_lore":
            char_name = (character or "主角").strip() or "主角"
            lore_dict = LifeSimPlugin._normalize_character_lore(target.get(key))
            seqs = [int(e.get("seq", 0)) for e in lore_dict.get(char_name, [])]
        else:
            seqs = [int(e.get("seq", 0)) for e in (target.get(key) or [])]
        return (max(seqs) if seqs else 0) + 1

    @staticmethod
    def _lore_label(key: str, section: str, character: str | None) -> str:
        if key == "character_lore":
            return f"{(character or '主角').strip() or '主角'} / {section}"
        return section

    @staticmethod
    def _normalize_character_lore(raw) -> dict:
        """把历史 list 结构(单角色)迁移为 dict {角色名: [entries]}。

        旧数据 [{section, content, updated_at}] → {"主角": [...]}
        已是 dict 原样返回。空 / None 视为 {"主角": {}}。
        """
        if raw is None:
            return {"主角": []}
        if isinstance(raw, list):
            if not raw:
                return {"主角": []}
            # 兼容:旧条目里若已带 character 字段,按其分组;否则统一归到「主角」。
            migrated: dict = {}
            for e in raw:
                if not isinstance(e, dict):
                    continue
                name = (e.get("character") or "主角").strip() or "主角"
                entry = {k: v for k, v in e.items() if k != "character"}
                migrated.setdefault(name, []).append(entry)
            return migrated
        if isinstance(raw, dict):
            return raw
        return {"主角": []}

    def _snapshot_lore(self, session: dict, turn: int):
        """在 turn 处快照当前 lore 状态,供 /undo 回滚。

        每个 turn 开始时(LLM 调用前)调用一次。/undo 时用 turn 计数回滚,
        比 msg_index 更稳定 — 压缩 / 增删消息不影响 turn 计数。

        深拷贝避免后续修改 session.lore 影响快照。
        """
        snapshots = session.setdefault("lore_snapshots", [])
        char_lore_dict = self._normalize_character_lore(session.get("character_lore"))
        char_lore_copy = {
            name: [dict(e) for e in entries] for name, entries in char_lore_dict.items()
        }
        snapshots.append(
            {
                "turn": turn,
                "world_lore": [dict(e) for e in (session.get("world_lore") or [])],
                "character_lore": char_lore_copy,
            }
        )

    async def _snapshot_narrative_history(
        self, session: dict, turn: int, scope: str
    ) -> None:
        """快照剧情历史状态(供 /undo 回滚)。

        每次 turn 开始时调用(LLM 调用前)抓取当前 scope 的所有记录。
        存储的是轻量级 (id, narrative, revised_count, revised_at) 元组 —
        不存世界设定 / 角色设定快照(那些与 lore 同步,lore 回滚已覆盖)。

        限制:最多保留 25 个快照,与 lore / rpg 一致。
        """
        records = await self.narrative_store.list(scope)
        light = [
            {
                "id": r["id"],
                "narrative": r.get("narrative", ""),
                "revised_count": int(r.get("revised_count", 0)),
                "revised_at": r.get("revised_at", ""),
            }
            for r in records
        ]
        snapshots = session.setdefault("narrative_snapshots", [])
        snapshots.append({"turn": turn, "scope": scope, "records": light})
        if len(snapshots) > 25:
            del snapshots[: len(snapshots) - 25]

    async def _restore_narrative_history(self, scope: str, snap: dict) -> dict:
        """从快照恢复剧情历史。返回 {"deleted": int, "restored": int}。"""
        target_ids = {r["id"] for r in snap.get("records", [])}
        target_map = {r["id"]: r for r in snap.get("records", [])}
        current = await self.narrative_store.list(scope)

        deleted = 0
        for r in current:
            if r["id"] not in target_ids and await self.narrative_store.delete(
                scope, r["id"]
            ):
                deleted += 1

        restored = 0
        for tid, state in target_map.items():
            ok = await self.narrative_store.restore(
                scope,
                {
                    "id": tid,
                    "narrative": state["narrative"],
                    "revised_count": state["revised_count"],
                    "revised_at": state["revised_at"],
                },
            )
            if ok:
                restored += 1

        return {"deleted": deleted, "restored": restored}

    # ─── 剧情历史自动记录 ─────────────────────────────────

    def _make_narrative_summary(self, text: str) -> str:
        """从剧情文本自动生成一行短摘要,用于 /历史 列表展示。"""
        if not text:
            return "(空)"
        # 优先第一个 ## 标题;否则第一个非空段
        for line in text.splitlines():
            s = line.strip()
            if s.startswith(("## ", "# ")):
                return s.lstrip("# ").strip()[:60]
        for line in text.splitlines():
            s = line.strip()
            if s:
                return (s[:60] + "…") if len(s) > 60 else s
        return text[:60]

    async def _auto_record_narrative(
        self,
        session: dict,
        scope: str,
        user_action: str,
        narrative: str,
    ) -> str | None:
        """把本轮成功输出的剧情写入独立存储。

        返回写入的 record_id;异常(磁盘满等)静默吞掉并 log,不影响主流程。
        """
        try:
            cleaned_action = _strip_xml_tags(user_action).strip()
            char_lore = self._normalize_character_lore(session.get("character_lore"))
            payload = {
                "user_action": cleaned_action[:500],
                "summary": self._make_narrative_summary(narrative),
                "narrative": narrative,
                "world_setting": session.get("world_setting", "") or "",
                "character_lore": {
                    name: [dict(e) for e in entries]
                    for name, entries in char_lore.items()
                },
                "world_lore": [dict(e) for e in (session.get("world_lore") or [])],
                "source_session_key": session.get("_session_key", scope),
                "mode": session.get("mode", "A"),
            }
            return await self.narrative_store.append(scope, payload)
        except (OSError, ValueError, TypeError) as e:
            logger.warning(f"life-sim: 剧情历史记录失败: {e}")
            return None

    # ─── lore 渲染 ──────────────────────────────────────

    def _build_lore_addendum(self, session: dict) -> str:
        """构造注入到 system prompt 的 lore 附加段。

        按 (角色 / section) 分组,每组的条目按 seq 升序排列成时间轴,
        每条标注 `[#seq | timestamp]`。新条目永远追加,旧细节永不被覆盖。

        在块顶部加粗体权威性声明,`appearance` 等硬约束 section 前面插入
        「禁止脑补」警告,强化模型对这些字段的遵从度。
        """
        HARD_SECTIONS = {"appearance", "forms"}
        parts = []
        world_lore = session.get("world_lore") or []
        if world_lore:
            lines = [
                "## 持久化世界观(按时间轴排列,自动注入每次对话)",
                "**⚠️ 以下世界观设定为本局唯一权威事实,叙事必须严格遵循,严禁凭印象修改、补充或「修正」。**",
            ]
            lines.extend(self._render_lore_timeline(world_lore))
            parts.append("\n".join(lines))
        char_lore_dict = self._normalize_character_lore(session.get("character_lore"))
        if any(char_lore_dict.values()):
            lines = [
                "## 持久化角色设定(按时间轴排列,自动注入每次对话)",
                "**⚠️ 以下角色设定为本局唯一权威事实。描写任何角色前必须先回扫本块,严格按字段值写。**",
                "**外貌(发色/瞳色/发型/服装/配饰/体型等)为硬性约束 — 严禁凭训练印象脑补、换色或「合理化」,除非本块末尾有变更条目明确覆盖。**",
            ]
            for char_name in sorted(char_lore_dict.keys()):
                entries = char_lore_dict[char_name]
                if not entries:
                    continue
                lines.append(f"### {char_name}")
                lines.extend(
                    self._render_lore_timeline(
                        entries, indent="- ", hard_sections=HARD_SECTIONS
                    )
                )
            parts.append("\n".join(lines))
        return "\n\n".join(parts)

    @staticmethod
    def _render_lore_timeline(
        entries: list, indent: str = "", hard_sections: set[str] | None = None
    ) -> list[str]:
        """把 (角色 / 世界观) 的 entries 列表渲染成时间轴字符串列表。

        按 section 分组,组内按 seq 升序,每条标注 `[#seq | timestamp]`。
        返回每行已加好 `indent` 前缀的字符串,直接 extend 进块。

        `hard_sections` 指定的 section(如 appearance / forms)在首条前会插入
        一行「禁止脑补」警告,强化模型对这些字段的遵从度。
        """
        hard_sections = hard_sections or set()
        sorted_entries = sorted(
            entries,
            key=lambda e: (
                str(e.get("section", "")),
                int(e.get("seq", 0)),
            ),
        )
        lines: list[str] = []
        prev_section: str | None = None
        for e in sorted_entries:
            sec = e.get("section", "")
            seq = e.get("seq", "?")
            ts = e.get("updated_at", "")
            content = e.get("content", "")
            if sec != prev_section and sec in hard_sections:
                lines.append(
                    f"{indent}> 🔒 **「{sec}」为硬性约束 — 发色/瞳色/服装/配饰等严禁凭印象脑补,叙事必须照写。**"
                )
            lines.append(f"{indent}[#{seq} | {ts}] **{sec}** — {content}")
            prev_section = sec
        return lines

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
            section(string): 分类标签,如 "魔法体系"、"主要势力"、"地理"。默认 "general"。每次调用追加一条带 seq + 时间戳的新记录,永不覆盖已有条目;同一 section 多次调用会累积成时间轴。
        Returns:
            确认消息。
        """
        return await self._save_lore(event, "world_lore", section, content)

    async def life_sim_save_character_lore(
        self,
        event,
        content: str,
        section: str = "general",
        character: str = "主角",
    ) -> str:
        """
        永久保存角色设定(支持多角色,按 character 分组累积)

        适用场景:
        - 形态变化(变身 / 进化 / 解锁新形态 / 退化)
        - 外貌变化(受伤 / 服装 / 装饰 / 年龄增长)
        - 性格变化(觉醒 / 黑化 / 成长 / 信念改变)
        - 重要记忆 / 关系变化
        - 习得技能 / 称号 / 职业变更
        - 重要 NPC 的设定

        Args:
            content(string): 角色设定内容(详细描述)
            section(string): 分类标签,如 "forms"、"appearance"、"personality"、"relationships"、"skills"。默认 "general"。每次调用追加一条带 seq + 时间戳的新记录,永不覆盖已有条目;同一 (character, section) 多次调用会累积成时间轴。
            character(string): 角色名。默认 "主角"。可用 NPC 真名 / 称号区分。
        Returns:
            确认消息。
        """
        return await self._save_lore(
            event, "character_lore", section, content, character=character
        )

    async def life_sim_revise_narrative(
        self,
        event,
        narrative: str,
        record_id: str = "",
    ) -> str:
        """
        覆盖更新一条已记录的剧情(用于「用户反馈剧情不对 → 重写」场景)。

        适用场景:
        - 用户反馈"这段写得不对 / 走向有问题",你重新输出剧情后,应调本工具
          用最新文本覆盖上一条记录,保持剧情历史与用户最终看到的版本一致
        - 本工具**只覆盖 narrative 字段**;world_setting / character_lore / world_lore
          等快照字段保留(它们记录的是写入时刻的世界观,不应被修改)
        - revised_at 会被自动更新为当前时间

        Args:
            narrative(string): 新的剧情完整文本(整段替换,不是追加)
            record_id(string, optional): 要覆盖的剧情记录 ID。
                - 留空 / 不传 / 传 `"last"` / 传 `"prev"` / 传 `"latest"` → 自动取当前 scope 的最新一条
                - 传具体 ID(如 `n_a1b2c3d4`) → 覆盖那一条
                - 当前会话的最近 ID 已在 system prompt 「📌 最近剧情ID」段给出,直接复制即可
        Returns:
            成功 / 失败消息。
        """
        scope = self._sim_session_key(event)
        if not narrative or not isinstance(narrative, str):
            return "❌ narrative 不能为空"

        # 解析 record_id:留空 / "last" / "latest" / "prev" 都取最新一条
        # 用 session.last_narrative_id 精准定位 — list() 按 created_at 排序但只到秒,
        # 同秒创建的记录顺序不稳定;session 里的字段由 append 时即时写入,永远指向真正的最后一条
        resolved_id = (record_id or "").strip()
        auto = False
        if not resolved_id or resolved_id.lower() in {"last", "latest", "prev", "previous"}:
            session = await self._load_sim(event)
            resolved_id = (session or {}).get("last_narrative_id") or ""
            auto = True
            if not resolved_id:
                return "❌ 当前 scope 暂无最近剧情 ID(从未记录过剧情),无法修订"

        ok = await self.narrative_store.revise(scope, resolved_id, narrative)
        if ok:
            # 标记本轮已 revise — 避免 _auto_record_narrative 把修订后的
            # 文本再次当成"新一轮"记录,造成内容几乎相同的重复记录
            self._pending_revise[scope] = True
            mode_note = "(自动取最近一条)" if auto else ""
            return (
                f"✅ 已覆盖剧情 `{resolved_id}` {mode_note}\n"
                f"   narrative 字段已替换,revised_at 已更新;world_setting / lore 快照保留\n"
                f"💡 本轮 text 响应不会再额外记录为新剧情,如需继续推进请用 /do"
            )
        return f"❌ 找不到记录 `{resolved_id}`(可能已被删除或 scope 不匹配)"

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
        lock = self._get_sim_lock(self._sim_session_key(event))
        if lock.locked():
            yield event.plain_result(self._busy_message())
            return

        async with lock:
            async for _ in self._cmd_create_body(event):
                yield _

    async def _cmd_create_body(self, event: AstrMessageEvent):
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
                except (ValueError, KeyError, TimeoutError, OSError) as e:
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
            + startup_steps
            + "最后,这一轮**不要**给出人生总结,故事需要用户多次推进"
        )

        yield event.plain_result(
            f"🎬 命运开始转动 [模式 {mode} - {MODE_NAMES[mode]}],正在编织你的人生..."
        )
        result = await self._generate(event, session, first_input, mode, imgs)
        yield event.plain_result(result)

    @filter.command("do", alias={"input", "输入"})
    async def cmd_input(self, event: AstrMessageEvent):
        """/do <选项/自定义行动/反馈> - 继续推进模拟"""
        lock = self._get_sim_lock(self._sim_session_key(event))
        if lock.locked():
            yield event.plain_result(self._busy_message())
            return

        async with lock:
            async for _ in self._cmd_input_body(event):
                yield _

    async def _cmd_input_body(self, event: AstrMessageEvent):
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
        except TypeError as e:
            yield event.plain_result(f"❌ 序列化失败:{e}")
            return

        header = (
            f"📦 session dump ({'full' if full else 'summary'})"
            f"  key=`{self._sim_session_key(event)}`"
        )
        if len(text) > 3000:
            logger.info(text)
            yield event.plain_result(
                f"{header}\n(内容过长,{len(text)} 字符,完整 dump 已写入日志)"
            )
            event.stop_event()
            return
        # 头部 + 内容,避免太长看不到 key
        yield event.plain_result(f"{header}\n{text}")

    @filter.command("删除")
    async def cmd_delete(self, event: AstrMessageEvent):
        """/删除 - 删除当前会话(同时清理对应 RPG 存档)"""
        lock = self._get_sim_lock(self._sim_session_key(event))
        if lock.locked():
            yield event.plain_result(self._busy_message())
            return

        async with lock:
            session = await self._load_sim(event)
            if not session:
                yield event.plain_result("❌ 当前没有进行中的转生模拟。")
                return

            group_id = self._get_group_id(event)
            sender_uid = str(event.get_sender_id() or "")
            try:
                purge = self.rpg_store.purge_group(group_id, sender_uid)
            except OSError as e:
                logger.debug(f"life-sim: 清理 RPG 存档失败: {e}")
                purge = {"deleted_chars": 0, "deleted_sessions": []}

            await self._clear_sim(event)
            char_note = (
                f",{purge['deleted_chars']} 个 RPG 存档"
                if purge["deleted_chars"]
                else ""
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
            return

    @filter.command("undo")
    async def cmd_undo(self, event: AstrMessageEvent):
        """/undo [N] - 撤销最近 N 轮对话(默认 1)。叙事历史、持久化 lore、RPG 数值(HP/EXP/装备/会话)全部回滚"""
        lock = self._get_sim_lock(self._sim_session_key(event))
        if lock.locked():
            yield event.plain_result(self._busy_message())
            return

        async with lock:
            async for _ in self._cmd_undo_body(event):
                yield _

    async def _cmd_undo_body(self, event: AstrMessageEvent):
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

        # 回滚剧情历史(新增的删掉、被修订的还原)
        narr_snapshots = session.get("narrative_snapshots") or []
        target_narr_snap = next(
            (s for s in reversed(narr_snapshots) if s.get("turn") == target_turn),
            None,
        )
        narr_stats = None
        scope = self._sim_session_key(event)
        if target_narr_snap is not None:
            narr_stats = await self._restore_narrative_history(scope, target_narr_snap)
        session["narrative_snapshots"] = [
            s for s in narr_snapshots if s.get("turn", 0) <= target_turn
        ]
        # last_narrative_id 若指向被删除的记录,清空(下次 /do 会重写)
        if narr_stats and narr_stats.get("deleted", 0) > 0:
            remaining = await self.narrative_store.list(scope)
            last_id = remaining[-1]["id"] if remaining else None
            if last_id != session.get("last_narrative_id"):
                session["last_narrative_id"] = last_id

        session["messages"] = messages
        await self._save_sim(event, session)

        # 统计
        user_n = sum(1 for m in removed if m.get("role") == "user")
        asst_n = sum(1 for m in removed if m.get("role") == "assistant")
        tool_n = sum(1 for m in removed if m.get("role") == "tool")
        summary_n = sum(1 for m in removed if m.get("_summary"))
        # 同时显示消息数和剧情记录数,避免混淆(每轮 user+assistant 是 2 条消息,
        # 但只对应 1 条剧情记录)
        remaining_narr = len(await self.narrative_store.list(scope))
        lines = [
            f"⏪ 已撤销最近 {user_n} 轮对话(删 {len(removed)} 条消息)",
            f"   组成:user × {user_n}, assistant × {asst_n}, tool × {tool_n}"
            + (f", summary × {summary_n}" if summary_n else ""),
            f"   剩余:{len(messages)} 条消息,{remaining_narr} 条剧情记录",
        ]
        if lore_restored:
            target_snap = next(
                (s for s in reversed(snapshots) if s["turn"] == target_turn), None
            )
            if target_snap is not None:
                w_n = len(target_snap["world_lore"])
                char_dict = target_snap["character_lore"] or {}
                c_n = sum(len(v) for v in char_dict.values() if isinstance(v, list))
                c_chars = sum(1 for v in char_dict.values() if v)
                if w_n or c_n:
                    parts = []
                    if w_n:
                        parts.append(f"世界观 {w_n} 条")
                    if c_n:
                        parts.append(f"角色 {c_n} 条(共 {c_chars} 名)")
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
        if narr_stats is not None:
            restored = narr_stats["restored"]
            deleted = narr_stats["deleted"]
            if restored or deleted:
                parts = []
                if restored:
                    parts.append(f"还原 ×{restored}")
                if deleted:
                    parts.append(f"删除 ×{deleted}")
                lines.append(f"   📖 剧情历史已回滚:{', '.join(parts)}")
            else:
                lines.append("   📖 剧情历史已回滚(无变化)")
        else:
            lines.append("   ⚠️ 未找到该 turn 的剧情快照(剧情历史未回滚)")
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

    # ════════════════════════════════════════════════════════════════
    # 剧情历史:列表 / 上传 / 删除
    # ════════════════════════════════════════════════════════════════

    @filter.command("历史")
    async def cmd_history(self, event: AstrMessageEvent):
        """/历史 [N] - 列出当前会话最近的 N 条剧情记录(默认 10)"""
        arg = self._extract_after_cmd(event, "历史").strip()
        n = 10
        if arg:
            try:
                n = int(arg.split()[0])
            except ValueError:
                yield event.plain_result("❌ 用法:`/历史 [N]`,N 为正整数")
                return
            if n < 1 or n > 200:
                yield event.plain_result("❌ N 必须在 1-200 之间")
                return

        scope = self._sim_session_key(event)
        records = await self.narrative_store.list(scope)
        if not records:
            yield event.plain_result("📭 当前会话暂无剧情历史(每轮 /do 输出会自动记录)")
            return

        recent = records[-n:]
        lines = [
            f"📜 当前 scope=`{scope}`,共 {len(records)} 条记录,展示最近 {len(recent)} 条:\n"
        ]
        for i, r in enumerate(recent, start=len(records) - len(recent) + 1):
            rid = r.get("id", "?")
            ts = r.get("created_at", "")
            rs = r.get("revised_at", "")
            revised_mark = " *(已修订)*" if rs and rs != ts else ""
            summary = (r.get("summary") or "(无摘要)").replace("\n", " ")
            action = (r.get("user_action") or "")[:40].replace("\n", " ")
            lines.append(f"**{i}. `{rid}`**{revised_mark}  {ts}")
            lines.append(f"   📝 {summary}")
            if action:
                lines.append(f"   💬 {action}")
        lines.append(
            "\n💡 导出文件:`/上传历史 [jsonl] [last N]` · 删除:`/删除历史 <id>|all`"
        )
        yield event.plain_result("\n".join(lines))

    @filter.command("上传历史")
    async def cmd_upload_history(self, event: AstrMessageEvent):
        """/上传历史 [jsonl] [last N|all] - 把剧情历史导出为文件并发送。
        默认导出当前 scope 全部记录,JSON 格式(含世界设定/角色设定快照)。
        jsonl:每条记录一行,便于分批读取
        last N:仅导出最近 N 条
        all:导出所有 scope 的记录(本用户/本群能访问到的全部)
        """
        arg = self._extract_after_cmd(event, "上传历史").strip().lower()
        use_jsonl = "jsonl" in arg
        scope = self._sim_session_key(event)

        # 解析 last N / all
        want_all = "all" in arg.split() or "all" == arg
        last_n = None
        for tok in arg.split():
            if tok.isdigit():
                last_n = int(tok)
                break

        if want_all:
            records = await self.narrative_store.list_all_for_owner(
                str(event.get_sender_id() or ""),
                current_scope=scope,
            )
            scope_label = "all"
        else:
            records = await self.narrative_store.list(scope)
            scope_label = scope

        if not records:
            yield event.plain_result(
                f"📭 scope=`{scope_label}` 暂无剧情历史,先 /创建 + 几轮 /do 再来。"
            )
            return

        if last_n is not None and last_n > 0:
            records = records[-last_n:]

        # 取最近的 world_setting / lore 快照作为"当前生效设定"放在文件顶部
        latest = records[-1]
        world_setting = latest.get("world_setting", "")
        character_lore = latest.get("character_lore", {}) or {}
        world_lore = latest.get("world_lore", []) or []

        ts_stamp = time.strftime("%Y%m%d_%H%M%S")
        # 把 scope 中的非 ASCII / 路径不安全字符压成 ASCII,避免文件名解析问题
        safe_scope = (
            scope_label.encode("ascii", "replace").decode("ascii").replace("?", "_")
            or "all"
        )
        filename = (
            f"narrative_{safe_scope}_{ts_stamp}.{'jsonl' if use_jsonl else 'json'}"
        )

        out_path = os.path.join(self.data_dir, filename)

        # 文件写入单独 try,失败时立即报错退出(不要让 file 组件去读半截文件)
        try:
            if use_jsonl:
                preamble = {
                    "_meta": {
                        "format": "jsonl",
                        "format_version": 1,
                        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "scope": scope_label,
                        "record_count": len(records),
                    },
                    "world_setting": world_setting,
                    "character_lore": character_lore,
                    "world_lore": world_lore,
                }
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(json.dumps(preamble, ensure_ascii=False) + "\n")
                    for r in records:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
            else:
                payload = {
                    "_meta": {
                        "format": "json",
                        "format_version": 1,
                        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "scope": scope_label,
                        "record_count": len(records),
                        "source_session_keys": sorted(
                            {r.get("source_session_key", "") for r in records}
                        ),
                    },
                    "world_setting": world_setting,
                    "character_lore": character_lore,
                    "world_lore": world_lore,
                    "records": [
                        {
                            "id": r.get("id"),
                            "seq": i + 1,
                            "created_at": r.get("created_at"),
                            "revised_at": r.get("revised_at"),
                            "revised": int(r.get("revised_count", 0)) > 0,
                            "scope": r.get("scope"),
                            "source_session_key": r.get("source_session_key"),
                            "user_action": r.get("user_action", ""),
                            "summary": r.get("summary", ""),
                            "narrative": r.get("narrative", ""),
                        }
                        for i, r in enumerate(records)
                    ],
                }
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
        except (OSError, TypeError, ValueError) as e:
            logger.warning(f"life-sim: 写剧情历史文件失败: {e}")
            try:
                os.remove(out_path)
            except OSError:
                pass
            yield event.plain_result(f"❌ 导出文件失败:{e}")
            return

        # 防御性校验:确认文件确实有内容(之前的 anyio bug 会导致文件 0 字节)
        size_bytes = os.path.getsize(out_path)
        if size_bytes == 0:
            logger.warning("life-sim: 写完文件大小为 0,异常")
            yield event.plain_result("❌ 导出文件为空,请重试")
            try:
                os.remove(out_path)
            except OSError:
                pass
            return
        size_kb = size_bytes / 1024
        try:
            from astrbot.core.message.components import File

            # 关键:用 framework 提供的临时文件跟踪机制,而不是手动 os.remove。
            # chain_result 是异步发送的 — 我们 generator 退出后 AstrBot 才读文件,
            # 此时若立刻删文件会竞态丢失。framework 在事件处理完后统一清理。
            event.track_temporary_local_file(out_path)
            chain = [File(name=filename, file=out_path)]
            yield event.chain_result(chain)
            yield event.plain_result(
                f"📤 已导出 {len(records)} 条剧情记录 → `{filename}`({size_kb:.1f} KB)\n"
                f"   格式:{'JSONL(每条一行,便于分批读取)' if use_jsonl else 'JSON(单文件)'}\n"
                f"   scope:`{scope_label}`"
            )
        except ImportError:
            logger.warning("life-sim: File 组件不可用,保留本地文件")
            yield event.plain_result(
                f"⚠️ File 组件不可用,文件已写到 `{out_path}`({len(records)} 条,{size_kb:.1f} KB)"
            )

    @filter.command("删除历史")
    async def cmd_delete_history(self, event: AstrMessageEvent):
        """/删除历史 <id|all> - 删除指定剧情记录,或 all 删除当前 scope 全部"""
        arg = self._extract_after_cmd(event, "删除历史").strip()
        if not arg:
            yield event.plain_result(
                "❌ 用法:\n"
                "  `/删除历史 <id>`  — 删除指定 ID(如 `n_a1b2c3d4`)\n"
                "  `/删除历史 all`  — 删除当前 scope 全部记录"
            )
            return

        scope = self._sim_session_key(event)

        if arg.lower() in ("all", "全部"):
            n = await self.narrative_store.delete_scope(scope)
            yield event.plain_result(f"🗑️ 已清空 scope=`{scope}` 全部剧情记录({n} 条)")
            return

        # 单条删除
        target = arg.split()[0].strip()
        if not target:
            yield event.plain_result("❌ 请提供记录 ID")
            return
        ok = await self.narrative_store.delete(scope, target)
        if ok:
            yield event.plain_result(f"🗑️ 已删除剧情记录 `{target}`")
        else:
            yield event.plain_result(
                f"❌ 找不到记录 `{target}`(可能 ID 输错,或不在当前 scope)"
            )

    async def terminate(self):
        logger.info("life-sim: 插件已卸载")
