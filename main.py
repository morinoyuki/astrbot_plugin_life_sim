"""转生模拟器 AstrBot 插件 - 主入口
- 模式 A: 纯叙事(默认)
- 模式 B: 游戏世界 RPG(HP/等级/装备/经验) — 来自 rpg_tools.RPGMixin
- 模式 C: DND 跑团(RPG + D20 骰子) — 来自 dice.DiceMixin
- 独立上下文: 叙事历史 KV 存储 + 显式 contexts
- 4 个指令: /创建 /do /进度 /删除
"""

import asyncio
import copy
import json
import os
import re
import shutil
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

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
from astrbot.core.provider.entities import LLMResponse, TokenUsage
from astrbot.core.provider.func_tool_manager import PY_TO_JSON_TYPE
from astrbot.core.star.star_tools import StarTools
from astrbot.core.utils.quoted_message.extractor import QuotedMessageExtractor

from .avatar_store import AvatarStore
from .dice import DiceMixin
from .im_render.engine import render_narrative
from .md_to_image import MdToImageMixin
from .prompts import (
    CHAT_CARD_PROMPT,
    HELP_TEXT,
    MODE_DETECT_SYSTEM_PROMPT,
    MODE_NAMES,
    SUMMARY_SYSTEM_PROMPT,
    SYSTEM_PROMPTS,
    TYPOGRAPHY_CHAT_CARD,
    TYPOGRAPHY_TEXT,
    _keyword_detect_mode,
    _parse_mode_prefix,
)
from .rpg_tools import RPGMixin
from .storage_branch import BranchStore
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


def _chain_to_content_parts(chain) -> list:
    """把 AstrBot 的消息组件链(LLMResponse.result_chain.chain)转成 LLM content parts。

    - Plain → TextPart(text=...)
    - Image → 跳过(LLM 历史里 image 引用由 image_urls 通道单独走)
    - At / Reply / 其它 → 跳过(LLM 上下文不需要)
    - 已经是 TextPart / ThinkPart / ImageURLPart 的实例 → 原样保留
    """
    out: list = []
    for comp in chain or []:
        if comp is None:
            continue
        # 已是合法 content part
        if isinstance(comp, (TextPart, ThinkPart, ImageURLPart)):
            out.append(comp)
            continue
        # Plain → TextPart
        text = getattr(comp, "text", None)
        if isinstance(text, str):
            if text:
                out.append(TextPart(text=text))
            continue
        # 其它类型(Image / At / Reply 等)跳过,记录 debug
        logger.debug(
            f"life-sim: result_chain 含非 Plain 组件 {type(comp).__name__},已跳过"
        )
    return out


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


def _strip_meta_tags(text: str) -> str:
    """只剥掉系统注入标签(<system_reminder> / <narrative_ref>),保留用户真实输入。

    用于 /redo 恢复上一轮的原始输入:system_reminder 是运行时注入的(重新生成会
    再注入),narrative_ref 里的剧情 ID 已随回滚失效;而 <Quoted Message> 是用户
    引用的上下文,重新生成时应当保留。
    """
    for tag in ("system_reminder", "narrative_ref"):
        text = re.sub(rf"<{tag}>[\s\S]*?</{tag}>", "", text, flags=re.DOTALL)
    return text.strip()


_CHAR_ALIAS_PATTERN = re.compile(r"[（(【]\s*([^）)】]+?)\s*[）)】]")

# 常见末字:跳过 "小X" 昵称变体,避免 "小时/小花/小王" 等高频词误伤
_NICKNAME_PREFIX_SKIP = {
    # 末字常见(小时/小花/小王/小天…)与首字常见(小明/小美/小红/小龙…)
    "时",
    "花",
    "王",
    "小",
    "大",
    "天",
    "日",
    "月",
    "中",
    "上",
    "下",
    "一",
    "二",
    "三",
    "十",
    "子",
    "人",
    "生",
    "心",
    "头",
    "年",
    "里",
    "东",
    "西",
    "南",
    "北",
    "前",
    "后",
    "春",
    "夏",
    "秋",
    "冬",
    "明",
    "美",
    "丽",
    "红",
    "白",
    "黑",
    "刚",
    "强",
    "龙",
    "虎",
    "燕",
    "兰",
    "梅",
    "霞",
    "芳",
    "玲",
    "翠",
    "秀",
    "英",
    "杰",
}


def _char_aliases(name: str) -> list[str]:
    """从角色名提取活跃检测用的候选匹配词。

    覆盖昵称/简称场景:
    - 全名本身:"花原（小花）"、"坂田银时"、"梦娜1号"
    - 括号内别名:"花原（小花）" → "小花"
    - 去括号主干:"花原（小花）" → "花原"
    - 去编号后缀:"梦娜1号" → "梦娜"、"小兰2世" → "小兰"
    - 中文称呼截取(去分隔符后取末尾 2 字):"坂田银时" → "银时"、
      "导师·长者" → "长者"、"江户川柯南" → "柯南"、"孙悟空" → "悟空"
    - 昵称变体(小/阿/酱 × 首字/末字):"汐见花音" → 小音/阿音/音酱,
      "雪音" → 小雪/阿雪/雪酱

    长度 < 2 的词不参与(避免单字昵称如 "香" 命中 "香气/香蕉" 等误伤)。
    """
    aliases = [name]
    bracket_aliases: list[str] = []
    for m in _CHAR_ALIAS_PATTERN.finditer(name):
        a = m.group(1).strip()
        if a and a not in aliases:
            aliases.append(a)
            bracket_aliases.append(a)
    stem = _CHAR_ALIAS_PATTERN.sub("", name).strip()
    if stem and stem not in aliases:
        aliases.append(stem)
    # 去分隔符标点 + 编号后缀:"梦娜1号" → "梦娜"、"L2" → "L"
    clean = re.sub(r"[·・\-_—\s]", "", stem)
    clean = re.sub(r"\d+[号世代替型卷尾]", "", clean)
    clean = re.sub(r"\d+$", "", clean)
    if clean and clean not in aliases:
        aliases.append(clean)
    # 中文称呼常取「名」/尾字组合:干净名 ≥ 3 字时取末尾 2 字
    if len(clean) >= 3:
        tail2 = clean[-2:]
        if tail2 not in aliases:
            aliases.append(tail2)
    # 昵称变体(小/阿/酱 × 首字/末字):有括号别名时以括号内容为名,
    # 不用姓的主干首末字(如 "花原（小花）" → 小花)
    name_part = bracket_aliases[-1] if bracket_aliases else clean
    if name_part:
        first, last = name_part[0], name_part[-1]
        for variant in (
            f"小{first}",
            f"阿{first}",
            f"{first}酱",
            f"小{last}",
            f"阿{last}",
            f"{last}酱",
        ):
            # 常见高频「小X」保护:小+首字 用首字黑名单(小明/小美…),
            # 小+末字 用末字黑名单(小时/小花…);方向错开,互不连坐
            if variant == f"小{first}" and first in _NICKNAME_PREFIX_SKIP:
                continue
            if variant == f"小{last}" and last in _NICKNAME_PREFIX_SKIP:
                continue
            # 阿X / X酱 特异性高,不设黑名单
            if len(variant) >= 2 and variant not in aliases:
                aliases.append(variant)
    return [a for a in aliases if len(a) >= 2]


def _match_lore_characters(char_lore: dict, query: str) -> list[str]:
    """按查询词找到匹配的角色 key 列表(用于按需读取)。

    匹配优先级(宁多勿漏):
    1. 精确 key;
    2. 查询词是某角色的候选词(全名 / 括号别名 / 末 2 字称呼 / 昵称变体);
    3. 名称互相包含(查询词 ⊂ 角色名 或反之)。

    同一个人被拆成多个 key(如 "汐见花音" 与 "花音")时,传 "花音"
    会同时匹配到两个 → 读取工具返回它们的全部 lore。
    单字查询(len < 2)只走候选词匹配,不做名称包含,避免 "花" 误匹配所有含花角色。
    """
    q = (query or "").strip()
    if not q:
        return []
    q_low = q.lower()
    matches: list[str] = []
    for name in char_lore:
        if not name:
            continue
        # 候选词匹配(全名/括号别名/昵称变体),大小写不敏感
        if (
            any(a.lower() == q_low for a in _char_aliases(name))
            or len(q) >= 2
            and (name.lower() in q_low or q_low in name.lower())
        ):
            matches.append(name)
    return matches


def _normalize_character_query(character) -> list[str]:
    """把工具的 character 参数(单个字符串 / 字符串数组 / None)归一化为非空查询词列表。

    - None / 空串 / 空数组 → []
    - "花音" → ["花音"]
    - ["花音", "银时"] → ["花音", "银时"](去空白、去空项)
    """
    if isinstance(character, str):
        q = character.strip()
        return [q] if q else []
    if isinstance(character, (list, tuple)):
        out: list[str] = []
        for c in character:
            if isinstance(c, str) and c.strip():
                out.append(c.strip())
        return out
    return []


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


async def _extract_image_with_quoted(event: AstrMessageEvent) -> list[Image]:
    """取图片,当前消息无图时回退到**引用消息**的图片。

    手机端常无法在同一消息里同时发文字 + 图片(引用图片省去重新上传)。
    复用 QuotedMessageExtractor(event).images()(与 /create /do 相同的引用通道):
    它会把引用消息里的图片解析成可 LLM 读取的 URL / base64 / 本地路径。
    这里统一包成 Image 组件(convert_to_file_path 能处理以上全部形态)。
    """
    imgs = await _extract_image(event)
    if imgs:
        return imgs

    try:
        refs = await QuotedMessageExtractor(event=event).images()
    except Exception as e:
        logger.warning(f"life-sim: 解析引用图片失败: {e}")
        return []
    # resolver 可能对同一张图返回重复/本地路径,去重后构造 Image
    out: list[Image] = []
    seen: set[str] = set()
    for ref in refs or []:
        ref = (ref or "").strip()
        if not ref or ref in seen:
            continue
        seen.add(ref)
        out.append(Image(file=ref))
    return out


def _restore_images_from_content(content) -> list[Image]:
    """从已存储的 user 消息 content 里恢复图片(Image 组件列表)。

    /redo 时上一轮事件的图片已经过去了,但 `_llm_resp_to_messages` 会把图片
    以 `data:image/<fmt>;base64,...` 的 ImageURLPart 存进消息历史,这里反序列化回来。
    自适应任意 MIME(jpeg / png / webp / gif):
    - url 保留原 data URL(带正确的 MIME 声明,供 `_generate` 的 image_urls 直接传 LLM)
    - file 设为 base64://(供 MediaResolver 嗅探字节重写历史时解析)
    """
    from astrbot.core.message.components import Image

    imgs: list[Image] = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") != "image_url":
            continue
        url = ((part.get("image_url") or {}).get("url") or "").strip()
        if not url.startswith("data:image"):
            continue
        b64 = url.split(",", 1)[1] if "," in url else ""
        if not b64:
            continue
        imgs.append(Image(file=f"base64://{b64}", url=url))
    return imgs


def _build_quoted_tag(text: str):
    return f"<Quoted Message>\n{text}\n</Quoted Message>"


def _build_system_reminder(event: AstrMessageEvent) -> str:
    """构造系统提醒的 tag"""
    user_id = event.get_sender_id()
    user_nick = event.get_sender_name()

    return (
        f"<system_reminder>User ID: {user_id}, Nickname: {user_nick}</system_reminder>"
    )


def _read_all_bytes(path: str) -> bytes:
    """同步读取文件全部字节(供 asyncio.to_thread 在线程池中调用)。"""
    with open(path, "rb") as f:
        return f.read()


def _build_narrative_ref_tag(last_nid: str) -> str:
    """构造最近剧情 ID 的 tag(放在当轮 user 消息里,不进 system prompt)。

    为什么放 user 消息而不是 system prompt:
    - system prompt 是每轮请求的最长公共前缀,必须**字节级稳定**才能命中
      提供商的前缀缓存(DeepSeek / Kimi / GLM / OpenAI 等)。剧情 ID 每轮都变,
      一旦出现在 system prompt 里,会从该字节起让后面整段历史缓存全部失效。
    - user 消息每轮本来就不同,把易变的 ID 放这里零额外成本。
    - 标签用 <narrative_ref> 包裹,`_strip_xml_tags` 会自动剥掉,
      不会污染剧情历史记录的 user_action 字段。
    """
    return (
        f"<narrative_ref>最近剧情ID: `{last_nid}` — 这是你**上一段输出**对应的剧情记录 ID。"
        f"用户反馈那段剧情需要修改时,直接调 "
        f'life_sim_revise_narrative(record_id="{last_nid}", narrative="<新剧情全文>") 覆盖即可,'
        f"不必让用户复制 ID(也可省略 record_id 自动修订最近一条)。</narrative_ref>"
    )


def _compact_lore_versions(session: dict) -> None:
    """把 `lore_snapshots` 收敛为 `_lore_versions` 内容索引表(去重)。

    - 老会话中快照内联了整份 `world_lore` / `character_lore`,连续轮次内容相同
      时会产生大量重复数据(实测 25 轮里只有 5 组内容)。
    - 这里把每份不相同的内容存到 `session["_lore_versions"]` 表里一次,
      快照只保留 `{turn, version}` 轻量索引;同一内容被多个 turn 复用。
    - 兼容反向:快照若已带 `version` 引用则按其指向解析,旧格式内联字段也会照常迁移。

    快照窗口滑动时,此函数同时处理两类快照,结果仍保证 `_resolve_snapshot_lore` 一致。
    """
    old_versions = session.get("_lore_versions") or []
    snapshots = session.get("lore_snapshots") or []
    versions: list[dict] = []
    index: dict[tuple[str, str], int] = {}
    out: list[dict] = []
    for snap in snapshots:
        if not isinstance(snap, dict) or "turn" not in snap:
            out.append(snap)
            continue
        turn = snap["turn"]
        vi = snap.get("version")
        if isinstance(vi, int) and 0 <= vi < len(old_versions):
            prev = old_versions[vi]
            wl = prev.get("world_lore", [])
            cl = prev.get("character_lore", {})
        else:
            wl = snap.get("world_lore", [])
            cl = snap.get("character_lore", {})
        key = (
            json.dumps(wl, sort_keys=True, ensure_ascii=False),
            json.dumps(cl, sort_keys=True, ensure_ascii=False),
        )
        new_vi = index.get(key)
        if new_vi is None:
            new_vi = len(versions)
            index[key] = new_vi
            versions.append({"world_lore": wl, "character_lore": cl})
        out.append({"turn": turn, "version": new_vi})
    session["lore_snapshots"] = out
    if out:
        session["_lore_versions"] = versions
    else:
        session.pop("_lore_versions", None)


def _resolve_snapshot_lore(session: dict, snapshot: dict) -> tuple[list, dict]:
    """解析单个 lore 快照的 (world_lore, character_lore)。

    新格式快照只带 `version` 索引,从 `_lore_versions` 取内容;
    旧格式快照仍内联了整份内容,直接返回。
    """
    if not isinstance(snapshot, dict):
        return [], {}
    vi = snapshot.get("version")
    if isinstance(vi, int):
        versions = session.get("_lore_versions") or []
        if 0 <= vi < len(versions):
            v = versions[vi]
            return v.get("world_lore", []), v.get("character_lore", {})
    return (
        snapshot.get("world_lore", []),
        snapshot.get("character_lore", {}),
    )


def _compact_rpg_versions(session: dict) -> None:
    """把 `rpg_snapshots` 收敛为 `_rpg_versions` 内容索引表(去重)。

    RPG 快照里 `chars` / `sessions` 是整份角色档案副本,大多数 turn 里内容完全
    相同(实测 25 轮只有 1 组内容)。这里按 (chars, sessions) 内容寻址,相同内容
    只存一次;快照本身退化为 `{turn, scope, version}` 轻量引用。

    `scope` 每轮保留(记录当时触发者),不进版本表 —— 不同 sender 触发同一状态
    时共享同一版本,不重复存储。
    """
    old_versions = session.get("_rpg_versions") or []
    snapshots = session.get("rpg_snapshots") or []
    versions: list[dict] = []
    index: dict[str, int] = {}
    out: list[dict] = []
    for snap in snapshots:
        if not isinstance(snap, dict) or "turn" not in snap:
            out.append(snap)
            continue
        vi = snap.get("version")
        if isinstance(vi, int) and 0 <= vi < len(old_versions):
            body = old_versions[vi]
        else:
            body = {k: snap[k] for k in ("chars", "sessions") if k in snap}
        key = json.dumps(body, sort_keys=True, ensure_ascii=False)
        new_vi = index.get(key)
        if new_vi is None:
            new_vi = len(versions)
            index[key] = new_vi
            versions.append(body)
        ref: dict = {"turn": snap["turn"], "version": new_vi}
        scope = snap.get("scope")
        if scope is not None:
            ref["scope"] = scope
        out.append(ref)
    session["rpg_snapshots"] = out
    if out:
        session["_rpg_versions"] = versions
    else:
        session.pop("_rpg_versions", None)


def _resolve_rpg_snapshot(session: dict, snapshot: dict) -> dict:
    """解析单个 RPG 快照为 `_rpg_restore` 需要的完整 dict。

    - 新格式: `{turn, scope, version}` → 从 `_rpg_versions` 取 (chars, sessions)。
    - 旧格式: 内联了 chars/sessions,原样返回。
    """
    if not isinstance(snapshot, dict):
        return {}
    vi = snapshot.get("version")
    if isinstance(vi, int):
        versions = session.get("_rpg_versions") or []
        if 0 <= vi < len(versions):
            body = dict(versions[vi])
            scope = snapshot.get("scope")
            if scope is not None:
                body["scope"] = scope
            return body
    return dict(snapshot)

def _narrative_branch(session: dict | None) -> str:
    """从会话取当前剧情线(分支)名;空 = 主线(history.json)。

    老会话里 current_branch 可能是 `"主线"`(旧版自动保留分支语义,
    表示"处于主线的延续线上"),归一为空串,与主线 history.json 对齐。
    """
    if not session:
        return ""
    b = (session.get("current_branch") or "").strip()
    if b == "主线":
        return ""
    return b


# ══════════════════════════════════════════════════════════════
# LLM 用量统计上报
#
# 本插件通过 context.llm_generate() / context.tool_loop_agent() 直接调用 LLM,
# 不经过 AstrBot 内部 agent 子阶段(pipeline),因此 token 消耗不会被写入
# 全局 provider_stats 表 —— WebUI「数据统计」页的调用量/Token 曲线读的就是这张表。
#
# 方案:给 provider 实例挂一个幂等的 text_chat 包装器,配合 ContextVar 作用域标记,
# 只累计本插件发起的调用;每轮结束后通过 db_helper.insert_provider_stat()
# 以 agent_type="internal" 写入同一张表,从而完整融入系统级数据统计。
# ══════════════════════════════════════════════════════════════

_LLM_STATS_CTX: ContextVar[dict | None] = ContextVar("life_sim_llm_stats", default=None)


def _ensure_provider_stats_hook(prov) -> bool:
    """给 provider 实例安装 text_chat 统计包装器(幂等)。返回是否成功。"""
    if getattr(prov, "_life_sim_stats_hooked", False):
        return True
    orig = getattr(prov, "text_chat", None)
    if not callable(orig):
        return False

    async def hooked(*args, **kwargs):
        resp = await orig(*args, **kwargs)
        ctx = _LLM_STATS_CTX.get()
        if ctx is not None:
            ctx["calls"] += 1
            if getattr(resp, "role", "") == "err":
                ctx["errors"] += 1
            usage = getattr(resp, "usage", None)
            if usage is not None:
                try:
                    ctx["usage"] = ctx["usage"] + usage
                except (AttributeError, TypeError):
                    pass
        return resp

    try:
        prov.text_chat = hooked  # type: ignore[method-assign]
    except (AttributeError, TypeError):
        return False
    prov._life_sim_stats_hooked = True
    return True


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


class LifeSimPlugin(DiceMixin, RPGMixin, MdToImageMixin, Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir()
        # 角色头像存储
        self.avatar_store = AvatarStore(self.data_dir)
        # 文件存储实例(sim 会话 + RPG 数据 + 剧情历史 + 分支快照,各自独立模块)
        self.sim_store = SimStore(self.data_dir)
        self.rpg_store = RpgStore(self.data_dir)
        self.narrative_store = NarrativeStore(self.data_dir)
        self.branch_store = BranchStore(self.data_dir)
        # AstrBot 在配置存在时传入,缺失时为 None
        self.config = config
        # 每个会话(group/user)一把 asyncio.Lock,防止同一会话并发触发 _generate 造成竞态
        self._sim_locks: dict[str, asyncio.Lock] = {}
        # 运行时工具集缓存(懒构建)— 工具集在运行期不变,避免每轮重建 + 重复解析 docstring
        self._cached_tool_set = None
        # 工具调用期间的 lore 暂存:{event_key: {"world_lore": [...], "character_lore": {...}}}
        # 工具 handler 只写这里,_generate 结束时统一合并到 session 并落库,
        # 避免工具内 _load_sim 拿到新 dict B 后又被外层旧 dict A 全量覆写。
        self._pending_lore: dict[str, dict] = {}
        # 本轮 revise 暂存:{event_key: [修订前记录状态, ...]} — 本轮 LLM 是否调用过
        # life_sim_revise_narrative(列表非空即有);兼作 /undo 回滚修订的 pre-revision 数据
        self._pending_revise: dict[str, list] = {}
        # Markdown → 图片渲染引擎(config 驱动,惰性加载样式)
        self._md_init()

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

    async def _yield_narrative_result(self, event, text: str):
        """把叙事输出以文本或图片形式 yield(根据 chat_card_enable / output_as_image)。

        优先级:聊天卡片(IM 对白) > Markdown 转图(pillowmd) > 纯文本。
        图片走 framework 的临时文件跟踪,事件处理完后自动删除。
        """
        if self._cfg("chat_card_enable", False):
            try:
                sent = False
                async for item in self._chat_card_generate(text, event):
                    sent = True
                    yield item
                if sent:
                    return
            except Exception as e:
                logger.warning(f"life-sim: 聊天卡片渲染失败,回退: {e}")

        if self.md_should_render(text):
            try:
                path = await self.md_render_to_path(
                    text,
                    autoPage=bool(self._cfg("output_image_auto_page", True)),
                )
                event.track_temporary_local_file(path)
                yield event.image_result(path)
                return
            except Exception as e:
                logger.warning(f"life-sim: 叙事转图失败,回退纯文本: {e}")
        yield event.plain_result(text)

    # ════════════════════════════════════════════════════════════════
    # 聊天卡片(IM 对白样式) — 基于 im_render 新引擎
    # ════════════════════════════════════════════════════════════════

    def _chat_card_avatar_prompt(self, event=None) -> str:
        """生成角色头像列表提示,注入 system prompt,让 LLM 输出规范角色名。

        作用:头像按「角色名 + scope」存储(/头像 汐见小亚 <图>)。若 LLM 输出的
        角色名与头像名有出入(如头像叫「龟」,对白叫「小龟」;或输出简称「小亚」
        而非「汐见小亚」),渲染时就可能匹配不上。所以除了列出头像名单,
        还要明确要求 LLM **自行把对白角色名与名单逐一比对**,有出入时在对白
        标签上加 `av="头像名"` 显式指定,而不是指望渲染端的模糊匹配兜底。

        按当前会话 scope(群/私聊)列出本区头像,不跨群泄露。
        """
        try:
            store = getattr(self, "avatar_store", None)
            scope = self._sim_session_key(event) if event is not None else ""
            names = store.list_names(scope) if store else []
        except Exception as e:
            logger.warning(f"life-sim: 读取头像列表失败: {e}")
            names = []
        if not names:
            return ""
        lines = "\n".join(f"- {n}" for n in names)
        return (
            "\n\n## 📷 角色头像名单(每句对白前必须先核对)"
            "\n以下角色已设置头像:**每个 `<d>` 对白的 name 都要先与这份名单比对**,再按下述规则书写:"
            "\n1. **完全一致**:name 与名单中某项完全相同 → 直接写,自动匹配头像;"
            "\n2. **有出入(简称/昵称/别称)**:想用的名字与头像名不完全一致,"
            "但你能从名单中判断出对应哪一张 → 必须加 `av` 属性显式指定。"
            "判断依据:头像名是对白名的子串(如对白「小龟」↔ 名单「龟」)、"
            "对白名是头像名的省略(如「小亚」↔「汐见小亚」)、或语义上明显同一人。"
            "\n   例:名单里有「龟」而对白叫它「小龟」 → `<d name=\"小龟\" av=\"龟\">主人你回来啦~</d>`"
            " —— 气泡显示「小龟」,头像用「龟」;"
            "\n3. **自选借用**:让任意角色使用名单中某张头像(即使两者毫无关系),"
            "同样用 `av`,如 `<d name=\"阴影少女\" av=\"汐见小亚\">你是谁?</d>`;"
            "\n4. **确实无关**:与名单所有项都对不上 → 正常写 name,不要硬套 `av`,也不要使用名单以外的头像名。"
            "\n注意:`av` 后面的头像名必须是名单中的原文,不可自创;宁可多写 `av` 也不要让名字出入的头像匹配失败。"
            "\n可用头像名单：" + lines + "\n"
        )

    def _chat_card_avatars(self, event=None) -> dict:
        """构建 角色名 → 头像 映射(含默认头兜底),按当前 scope 过滤。"""
        avatars = {}
        try:
            store = getattr(self, "avatar_store", None)
            if store is not None:
                scope = self._sim_session_key(event) if event is not None else ""
                for name in store.list_names(scope):
                    avatars[name] = store.get_avatar(name, scope)
                d = store.get_default_avatar()
                if d:
                    avatars[""] = d
        except Exception as e:
            logger.warning(f"life-sim: 加载角色头像失败: {e}")
        return avatars

    def _temporary_avatar_copy(self, path: str, event) -> str | None:
        """把已存储的头像复制成临时文件,交给框架在事件结束后清理。

        不能直接把 avatar_store 里的真实头像文件登记为临时文件——框架会在
        事件结束后清理掉这些文件,那样「查看头像」反而会把用户的头像删掉。
        """
        import tempfile

        p: str | None = None
        try:
            fd, p = tempfile.mkstemp(suffix=os.path.splitext(path)[1] or ".png")
            os.close(fd)
            shutil.copyfile(path, p)
            event.track_temporary_local_file(p)
            return p
        except Exception as e:
            logger.warning(f"life-sim: 头像临时副本创建失败: {e}")
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
            return None

    async def _chat_card_generate(self, text: str, event):
        """把叙事 markdown 渲染为聊天截图并逐一 yield。"""
        from .im_render.engine import TooManyPages

        loops = asyncio.get_running_loop()

        def _build() -> list:
            return self._chat_card_render(text, event)

        try:
            images = await loops.run_in_executor(None, _build)
        except TooManyPages:
            logger.warning("life-sim: 聊天卡片分页超限,降级")
            return
        except Exception as e:
            logger.warning(f"life-sim: 聊天卡片渲染失败: {e}")
            return

        for img in images or []:
            if img is None:
                continue
            path = await self._save_chat_bubble(img)
            if path:
                event.track_temporary_local_file(path)
                yield event.image_result(path)

    def _chat_card_render(self, text: str, event=None) -> list:
        """同步执行渲染,返回图片列表。可被线程池调用。"""
        theme = str(self._cfg("chat_card_theme", "light") or "light").strip().lower()
        if theme not in ("light", "dark"):
            theme = "light"
        self_names = [
            s.strip()
            for s in str(self._cfg("chat_card_self_names", "我,自己,你,玩家")).split(
                ","
            )
            if s.strip()
        ]
        avatars = self._chat_card_avatars(event)

        def _is_self(speaker: str) -> bool:
            return speaker in self_names

        return render_narrative(
            text,
            theme=theme,
            width=int(self._cfg("chat_card_width", 1024)),
            font_size=int(self._cfg("chat_card_font_size", 34)),
            title=str(self._cfg("chat_card_title", "") or ""),
            max_pages=int(self._cfg("chat_card_max_pages", 5)),
            is_self=_is_self,
            avatars=avatars,
        )

    async def _save_chat_bubble(self, img) -> str:
        """保存聊天卡片图到临时文件并返回路径。"""
        import tempfile

        fd, p = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            await asyncio.get_running_loop().run_in_executor(None, img.save, p, "PNG")
            return p
        except Exception as e:
            logger.warning(f"life-sim: 聊天卡片保存失败: {e}")
            try:
                os.remove(p)
            except OSError:
                pass
            return None

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

    async def _clear_sim(self, event: AstrMessageEvent) -> int:
        """删除当前会话文件,并同步清理该会话名下全部分支快照与头像。

        分支快照独立存储(见 BranchStore),不会随会话文件自动消失;
        头像按 scope 分区(见 AvatarStore)。都会在删除/重建会话时显式清理。
        返回被清理的分支快照数量。
        """
        key = self._sim_session_key(event)
        await self.sim_store.delete(key)
        # 头像按 scope 分区,随会话一起清除(默认头像在根目录,不动)
        self.avatar_store.clear_scope(key)
        return await self.branch_store.delete_scope(key)

    def _busy_message(self) -> str:
        return "⏳ 上一条消息还在处理中,请稍候再试..."

    def _extract_after_cmd(
        self, event: AstrMessageEvent, cmds: str | tuple[str, ...] | list[str]
    ) -> str:
        """提取命令首次出现位置之后的所有内容。支持多个候选命令名(含 alias)。

        prefix 不再硬编码:AstrBot 的 @filter.command 会按系统配置识别 / ！ ~ 等
        (私聊可能无 prefix),找到命令字符串的位置之后的全部就是参数。

        必须支持 alias:如 /do 的命令别名有 input / 输入,用户发 `/输入 xxx` 时
        文本里没有 "do",只按 "do" 找会把参数丢光。
        """
        if isinstance(cmds, str):
            cmds = (cmds,)
        text = (event.message_str or "").strip()
        if not text:
            return ""
        best_idx, best_len = -1, 0
        for cmd in cmds:
            idx = text.find(cmd)
            while idx >= 0:
                # 命令必须出现在行首,或紧跟系统命令前缀(/ ！ ~ 等),
                # 避免误匹配正文中恰好包含该词的文本(如 "redo" 里的 "do")。
                if idx == 0 or (idx > 0 and text[idx - 1] in "/!～~"):
                    if best_idx < 0 or idx < best_idx:
                        best_idx, best_len = idx, len(cmd)
                    break
                idx = text.find(cmd, idx + 1)
        if best_idx < 0:
            return ""
        return text[best_idx + best_len :].strip()

    # ────────────────────────────────────────────────────────────────
    # 工具调用日志(hook 捕获 + 历史落盘)
    # ────────────────────────────────────────────────────────────────

    @staticmethod
    def _is_my_tool(name: str) -> bool:
        """过滤:只保留本插件的工具(rpg_*/roll_dice/life_sim_save_*/life_sim_get_*/life_sim_revise_narrative)。"""
        return bool(name) and (
            name.startswith("rpg_")
            or name
            in {
                "roll_dice",
                "life_sim_save_character_lore",
                "life_sim_save_world_lore",
                "life_sim_get_character_lore",
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

        运行时工具集不变,结果懒缓存到 self._cached_tool_set,避免每轮重建
        (重建要 dir(self) + 逐个 docstring_parser 解析 + 查 provider_manager)。
        """
        if self._cached_tool_set is not None:
            return self._cached_tool_set

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
        self._cached_tool_set = tool_set
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

        llm_resp = await self._run_llm_with_stats(
            event,
            pid,
            lambda: self.context.llm_generate(
                chat_provider_id=pid,
                system_prompt=MODE_DETECT_SYSTEM_PROMPT,
                contexts=[],
                prompt=user_msg,
            ),
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

        llm_resp = await self._run_llm_with_stats(
            event,
            pid,
            lambda: self.context.llm_generate(
                chat_provider_id=pid,
                system_prompt=SUMMARY_SYSTEM_PROMPT,
                contexts=contexts,
                prompt=prompt,
            ),
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
                ws = _strip_xml_tags(_content_to_text(m.get("content"))).strip()
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
        # 存库的 user 消息带 <system_reminder> / <narrative_ref> 等标签,先剥掉再收录
        user_actions = []
        for m in head_msgs:
            if m.get("role") == "user":
                a = _strip_xml_tags(_content_to_text(m.get("content"))).strip()
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

    async def _run_llm_with_stats(
        self,
        event: AstrMessageEvent | None,
        provider_id: str,
        fn: Callable[[], Awaitable[LLMResponse]],
    ) -> LLMResponse:
        """执行一次(或一轮 agent loop)LLM 调用,并把 token 用量写入 AstrBot 全局统计表。

        原理:在 ContextVar 作用域内调用 provider.text_chat 的统计包装器,
        累计本插件本轮所有请求的 usage;结束后通过 db_helper.insert_provider_stat()
        以 agent_type="internal" 写入 provider_stats 表 —— 与 WebUI「数据统计」页
        同一数据源,因此调用量/Token 曲线会把本插件的消耗计入。
        统计失败不影响主流程。旧版 AstrBot 无此接口时静默跳过。
        """
        # 确保 provider 实例已安装 text_chat 统计包装器(幂等)
        if self._cfg("record_llm_stats", True):
            try:
                prov = await self.context.provider_manager.get_provider_by_id(
                    provider_id
                )
                if prov is not None:
                    _ensure_provider_stats_hook(prov)
            except Exception as e:
                logger.debug(f"life-sim: 安装 LLM 用量统计钩子失败: {e}")

        ctx = {
            "calls": 0,
            "errors": 0,
            "usage": TokenUsage(),
            "start_time": time.time(),
            "end_time": 0.0,
        }
        token = _LLM_STATS_CTX.set(ctx)
        errored = False
        try:
            llm_resp = await fn()
            errored = getattr(llm_resp, "role", "") == "err"
            return llm_resp
        except Exception:
            errored = True
            raise
        finally:
            _LLM_STATS_CTX.reset(token)
            ctx["end_time"] = time.time()
            if self._cfg("record_llm_stats", True):
                status = "error" if errored else "completed"
                try:
                    await self._flush_llm_provider_stat(
                        event, provider_id, ctx, status
                    )
                except Exception as e:
                    logger.debug(f"life-sim: 写入 LLM 用量统计失败(不影响功能): {e}")

    async def _flush_llm_provider_stat(
        self,
        event: AstrMessageEvent | None,
        provider_id: str,
        ctx: dict,
        status: str,
    ) -> None:
        """把一轮累计的用量写入 AstrBot 全局 provider_stats 表(WebUI 数据统计页数据源)。"""
        from astrbot.core import db_helper  # 局部导入避免启动顺序问题

        insert = getattr(db_helper, "insert_provider_stat", None)
        if insert is None:
            return  # 旧版 AstrBot,无此接口

        u = ctx.get("usage") or TokenUsage()
        provider_model = None
        try:
            prov = await self.context.provider_manager.get_provider_by_id(provider_id)
            get_model = getattr(prov, "get_model", None)
            provider_model = get_model() if callable(get_model) else None
        except Exception:
            provider_model = None

        await insert(
            umo=event.unified_msg_origin if event is not None else "",
            provider_id=provider_id or "",
            provider_model=provider_model,
            conversation_id=None,
            status=status if ctx.get("calls") else "error",
            stats={
                "token_usage": {
                    "input_other": int(getattr(u, "input_other", 0) or 0),
                    "input_cached": int(getattr(u, "input_cached", 0) or 0),
                    "output": int(getattr(u, "output", 0) or 0),
                },
                "start_time": float(ctx.get("start_time") or 0.0),
                "end_time": float(ctx.get("end_time") or 0.0),
                # 非流式调用拿不到真实 TTFT,置 0(统计页会忽略为 0 的样本)
                "time_to_first_token": 0.0,
            },
            agent_type="internal",
        )

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
        self._pending_revise[event_key] = []

        world_setting = session.get("world_setting")
        system_prompt_tpl = SYSTEM_PROMPTS.get(mode, SYSTEM_PROMPTS["A"])
        # 完整设定作为独立段落追加在 system prompt 末尾。
        system_prompt = system_prompt_tpl
        if world_setting:
            system_prompt += (
                f"\n\n## 本局世界观(全文,{len(world_setting)} 字)\n\n{world_setting}\n"
            )

        # 注入持久化 lore(角色设定 + 世界观信息,直到 /删除 或 /创建)
        lore = self._build_lore_addendum(session, user_input)
        if lore:
            system_prompt += "\n\n" + lore

        # 聊天卡片模式:先把公共排版段替换成聊天卡片专用排版(避免普通"合并短句"
        # 规则污染聊天输出),再注入对白输出规范
        if self._cfg("chat_card_enable", False):
            if TYPOGRAPHY_TEXT and TYPOGRAPHY_TEXT in system_prompt:
                system_prompt = system_prompt.replace(
                    TYPOGRAPHY_TEXT, TYPOGRAPHY_CHAT_CARD
                )
            system_prompt += CHAT_CARD_PROMPT
            system_prompt += self._chat_card_avatar_prompt(event)

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

        # ── turn 计数与快照:失败 / 空输出的 /do 不推进 ──
        # 旧实现:进入 LLM 调用前就递增 lore_turn 并拍快照;若本轮调用失败/返回空文本
        # (不落任何 user 消息),lore_turn 与用户消息数会错位 —— /undo N 按消息数回滚,
        # 却按 lore_turn 倒推目标轮,导致目标轮偏晚、剧情历史只删了一条。
        # 现在:RPG 快照内容(存档文件会被本轮工具就地修改)必须在调用前抓取;
        # lore / 剧情历史快照与 turn 递增移到 LLM 成功后统一提交(见下)。
        rpg_capture = (
            self._rpg_snapshot(event, mode) if mode in ("B", "C") else None
        )

        contexts = bind_checkpoint_messages(messages)

        # 从 config 读工具调用参数(模式 B/C 用)
        tool_max_steps = max(1, min(100, int(self._cfg("tool_max_steps", 30))))
        tool_call_timeout = max(10, min(300, int(self._cfg("tool_call_timeout", 60))))

        system_reminder = _build_system_reminder(event)

        user_input += system_reminder
        # 最近剧情 ID 注入到当轮 user 消息而不是 system prompt:
        # system prompt 必须字节级稳定才能命中前缀缓存(ID 每轮都变,放里面会把
        # 后面整段历史缓存打爆);user 消息每轮本来就不同,放这里零额外成本。
        # 模式 A 无工具可调 revise,不需要注入。
        if mode in ("B", "C"):
            last_nid = session.get("last_narrative_id")
            if last_nid:
                user_input += _build_narrative_ref_tag(last_nid)

        image_urls = [(img.url or img.path) for img in (imgs or [])]
        tool_hooks: _LifeSimToolHooks | None = None
        try:
            if mode == "A":
                llm_resp = await self._run_llm_with_stats(
                    event,
                    provider_id,
                    lambda: self.context.llm_generate(
                        chat_provider_id=provider_id,
                        system_prompt=system_prompt,
                        image_urls=image_urls,
                        contexts=contexts,
                        prompt=user_input,
                    ),
                )
            else:
                # 传 tools 让 LLM 知道 rpg_*/roll_dice 可用(否则 tool_loop_agent 不会调任何工具)
                tools = self._build_my_tool_set()
                tool_hooks = _LifeSimToolHooks()
                llm_resp = await self._run_llm_with_stats(
                    event,
                    provider_id,
                    lambda: self.context.tool_loop_agent(
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
                    ),
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

        # ── LLM 调用成功,提交本轮 turn:递增计数 + 快照 pre-turn 状态 ──
        turn = session.get("lore_turn", 0) + 1
        session["lore_turn"] = turn
        self._snapshot_lore(session, turn)
        # 同步快照剧情历史状态(供 /undo 回滚被本 turn 新增/修订的记录)。
        # 此时本轮记录尚未写入(`_auto_record_narrative` 在末尾才调),
        # 所以快照拿到的 `ids` 正是"本轮开始前"的记录集合;工具里的 revise
        # 只改写既有记录(id 不变),不改变 ids 集合。
        await self._snapshot_narrative_history(session, turn, event_key)
        # 同步快照 RPG 数值状态:内容已在 LLM 调用前抓取(rpg_capture),
        # 即 pre-turn 状态,这里只补 turn 号提交。
        if rpg_capture is not None:
            rpg_snaps = session.setdefault("rpg_snapshots", [])
            rpg_snaps.append({"turn": turn, **rpg_capture})
            # 限制最多保留 25 个快照(每个可能含多角色,避免 KV 膨胀)
            if len(rpg_snaps) > 25:
                del rpg_snaps[: len(rpg_snaps) - 25]
            # 去重:chars/sessions 内容寻址收敛到 `_rpg_versions` 索引表
            _compact_rpg_versions(session)

        # 给本轮 user 消息盖上 turn 戳,`/undo N` 按戳精确定位回滚目标轮
        # (消息与轮次一一对应,不受失败轮/压缩/摘要影响)。
        for _m in new_msgs:
            if isinstance(_m, dict) and _m.get("role") == "user":
                _m["turn"] = turn
                break

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
        # 同时把各次修订前的记录状态并入本轮快照(供 /undo 回滚,快照本身不存全文)。
        revise_states = self._pending_revise.pop(event_key, [])
        revise_called = bool(revise_states)
        if revise_states:
            for s in reversed(session.get("narrative_snapshots") or []):
                if s.get("turn") == turn:
                    s.setdefault("revised", []).extend(revise_states)
                    break

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
            from astrbot.core.utils.media_utils import MediaResolver

            for img in images:
                # 自适应图片 MIME:由 MediaResolver 嗅探字节,生成正确的
                # data:image/jpeg|png|webp|gif;base64,...(不再硬编码 png)。
                data_url = await MediaResolver(
                    img.url or img.file or img.path, media_type="image"
                ).to_data_url()
                if data_url:
                    content.append(
                        ImageURLPart(image_url=ImageURLPart.ImageURL(url=data_url))
                    )

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
            # result_chain.chain 是 AstrBot 的消息组件列表(Plain / Image / At / Reply 等),
            # 而 AssistantMessageSegment.content 需要 LLM content parts(TextPart / ThinkPart / ImageURLPart)。
            # 直接传组件会触发 pydantic 校验失败(content.str 期望 string,拿到 list of Plain)。
            # 这里把 Plain → TextPart,其它类型跳过(LLM 历史里不必要保留 @ / 回复结构)。
            final_content = _chain_to_content_parts(llm_resp.result_chain.chain)
        else:
            # tool_loop_agent 最终响应有时 result_chain 为 None;从 _completion_text 重建
            text = (getattr(llm_resp, "_completion_text", "") or "").strip()
            final_content = [TextPart(text=text)] if text else []

        # 思考部分统一前置(不管 result_chain 是否为空):从 reasoning_content 或
        # raw_completion 提取(OpenRouter 的 message.reasoning 块数组等不同格式),
        # 保证思考被存进历史、下一轮能回传给推理模型(否则 OpenAI 系 API 会用不到)。
        think, think_sig = self._extract_thinking(llm_resp)
        if think:
            final_content.insert(0, ThinkPart(think=think, encrypted=think_sig))

        if not final_content:
            final_content = [TextPart(text="(模型未输出文本)")]
        msgs.append(AssistantMessageSegment(content=final_content).model_dump())
        logger.debug(f"life-sim resp: {msgs[-1]}")
        return msgs

    @staticmethod
    def _extract_thinking(llm_resp: LLMResponse) -> tuple[str, str | None]:
        """从 LLMResponse 提取思考内容,兼容不同提供商格式。

        优先级:
        1. `llm_resp.reasoning_content` — DeepSeek / Kimi 等原生 reasoning_content 字段
        2. raw_completion 的 `message.reasoning` — **OpenRouter 格式**(内容块数组,
           `[{"type": "reasoning", "reasoning": "..."}]`,也可能有 nested reasoning block)
        3. raw_completion 的 `message.reasoning_content` / model_extra — 部分网关的兼容字段

        返回 (think_text, signature);无思考时返回 ("", None)。
        """
        think = (getattr(llm_resp, "reasoning_content", "") or "").strip()
        if think:
            return think, getattr(llm_resp, "reasoning_signature", None)

        raw = getattr(llm_resp, "raw_completion", None)
        if raw is None:
            return "", None
        try:
            choices = getattr(raw, "choices", None) or []
            if not choices:
                return "", None
            msg = getattr(choices[0], "message", None)
            if msg is None:
                return "", None
            extra = getattr(msg, "model_extra", None) or {}
            for key in ("reasoning", "reasoning_content"):
                val = getattr(msg, key, None)
                if val is None:
                    val = extra.get(key)
                if isinstance(val, list):
                    # OpenRouter:内容块数组
                    texts = []
                    for part in val:
                        if not isinstance(part, dict):
                            continue
                        if part.get("type") == "reasoning":
                            texts.append(str(part.get("reasoning", "")))
                        else:
                            # 兼容某些实现直接把文本放 content 字段
                            t = str(part.get("content", "") or "").strip()
                            if t:
                                texts.append(t)
                    if texts:
                        return "\n".join(t.strip() for t in texts if t.strip()), None
                elif isinstance(val, str) and val.strip():
                    return val.strip(), None
        except (AttributeError, IndexError, TypeError):
            pass
        return "", None

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
        比 msg_index 更稳定 —— 压缩 / 分支切换不影响 turn 计数。

        深拷贝避免后续修改 session.lore 影响快照。快照经 `_compact_lore_versions`
        内容寻址去重:连续多轮 lore 未变时,只保留一份较新版本引用而不是整份拷贝。
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
        # 与 rpg / narrative 快照一致,限制最多保留 25 个,避免会话 KV 无限膨胀。
        # undo 最大回滚 20 轮,25 个快照足够覆盖。
        if len(snapshots) > 25:
            del snapshots[: len(snapshots) - 25]
        # 去重 + 迁移旧格式(把内联整份 lore 收敛到 `_lore_versions` 索引表)
        _compact_lore_versions(session)

    async def _snapshot_narrative_history(
        self, session: dict, turn: int, scope: str
    ) -> None:
        """快照剧情历史状态(供 /undo 回滚)。

        每次 turn 开始时调用(LLM 调用前)抓取当前 scope 的所有记录。

        **只存记录 ID 列表,不再复制 narrative 全文** — 旧的实现每轮把全部记录
        的完整剧情文本再存一份(25 个快照 × 每轮增长的记录数 = O(n²) 重复数据,
        一次 /do 就能膨胀几十 KB)。
        - `ids`:全部记录 ID(轻量,用于回滚时判断哪些是快照点后新增、要删除)
        - `revised`:修订前的记录状态,由 `life_sim_revise_narrative` 在修订时
          暂存到本轮快照(只有修订发生的轮次才非空)

        限制:最多保留 25 个快照,与 lore / rpg 一致。
        """
        # 只存记录 ID 列表(轻量);快照标记所属 branch,回滚时按当前线隔离
        records = await self.narrative_store.list(scope, _narrative_branch(session))
        snapshots = session.setdefault("narrative_snapshots", [])
        snapshots.append(
            {
                "turn": turn,
                "scope": scope,
                "ids": [r["id"] for r in records],
                "revised": [],
            }
        )
        if len(snapshots) > 25:
            del snapshots[: len(snapshots) - 25]

    async def _restore_narrative_history(
        self,
        scope: str,
        snap: dict,
        all_snaps: list | None = None,
        branch: str = "",
    ) -> dict:
        """从快照恢复指定线(主线/分支)的剧情历史。返回 {"deleted": int, "restored": int}。

        删除:当前存在但快照点不存在(快照点后新增)的记录。
        回滚修订:
        - 旧格式快照(含 records 全量副本):全部写回;
        - 新格式快照:收集 **target_turn 及之后所有快照**的 `revised` 状态,
          按新→旧顺序取每个记录的最终 pre-revision 值(同一记录多次修订时
          链式回退才能回到快照点状态)。
        """
        old_records = snap.get("records")
        if old_records is not None:
            target_ids = {r["id"] for r in old_records}
            target_map = {r["id"]: r for r in old_records}
        else:
            target_ids = set(snap.get("ids") or [])
            target_map: dict = {}
            target_turn = snap.get("turn", 0)
            for s in reversed(all_snaps or []):
                if s.get("turn", 0) < target_turn:
                    break
                for state in s.get("revised") or []:
                    # 新→旧遍历,后写覆盖 → 同 id 最终保留最旧的 pre-revision 值,
                    # 即快照点的真实状态
                    target_map[state["id"]] = state
                # 兼容:跨版本混合的旧格式快照,records 本身是全量 light 副本,直接采入
                for state in s.get("records") or []:
                    target_map[state["id"]] = state

        current = await self.narrative_store.list(scope, branch)

        deleted = 0
        for r in current:
            if r["id"] not in target_ids and await self.narrative_store.delete(
                scope, r["id"], branch=branch
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
                branch=branch,
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
            return await self.narrative_store.append(
                scope, payload, branch=_narrative_branch(session)
            )
        except (OSError, ValueError, TypeError) as e:
            logger.warning(f"life-sim: 剧情历史记录失败: {e}")
            return None

    # ─── lore 渲染 ──────────────────────────────────────

    def _detect_active_characters(
        self, session: dict, rounds: int, extra_text: str = ""
    ) -> set[str]:
        """从最近 `rounds` 轮对话的文本 + 当前输入中检测出场过的角色名(启发式)。

        用于选择性加载:最近出场过的角色完整注入 system prompt,
        其余角色只保留一行提示。

        `extra_text` 传入**当前轮用户输入**(尚未写入历史):用户当场点名
        "主角见到了汐见花音" 时,该角色也能立即判活跃并完整注入,避免被裁剪。

        匹配策略:角色名的任一候选词(全名 / 括号内昵称 / 末 2 字称呼 / 昵称变体,长度≥2)
        在最近文本中(含当前输入)出现即判活跃 —— 例:"花原（小花）" 在上下文只提 "小花"
        也能命中;同一个人被分成多个 key(如 "花原（小花）" 与 "小花")时
        会一起判活跃,避免同一人设定被部分裁剪。

        启发式偏保守(**宁多勿漏**):活跃误判只是多注入一点 token,
        而漏判会导致该角色设定被裁剪、LLM 刻画时没有依据而抽风。
        """
        messages = session.get("messages", [])
        user_pos = [i for i, m in enumerate(messages) if m.get("role") == "user"]
        if user_pos:
            start = user_pos[-rounds] if len(user_pos) >= rounds else 0
            recent = messages[start:]
        else:
            recent = messages[-rounds * 2 - 2 :]
        text = "\n".join(
            _content_to_text(m.get("content"))
            for m in recent
            if m.get("role") in ("user", "assistant")
        )
        if extra_text:
            text += "\n" + extra_text
        char_lore = self._normalize_character_lore(session.get("character_lore"))
        active: set[str] = set()
        for name in char_lore:
            if any(alias in text for alias in _char_aliases(name)):
                active.add(name)
        return active

    def _build_lore_addendum(self, session: dict, current_input: str = "") -> str:
        """构造注入到 system prompt 的 lore 附加段。

        按 (角色 / section) 分组,每组的条目按 seq 升序排列成时间轴,
        每条标注 `[#seq | timestamp]`。新条目永远追加,旧细节永不被覆盖。

        `current_input` 为当前轮用户输入(尚未写入历史):活跃检测把它并入,
        用户当场点名某角色时会立即完整注入该角色设定。

        选择性加载(`lore_selective_load` 默认开,`lore_active_rounds` 默认 6):
        - 角色:最近 N 轮出场过的角色完整注入;其余角色只注入一行提示
          (名字 + 条目数 + 按需读取工具),刻画前由 LLM 调
          `life_sim_get_character_lore` 获取完整设定 → 角色多、轮数多时
          system prompt 不再被整棵角色树撑爆。
        - 世界观:不做按需裁剪,一律完整注入(历史条目本身有参考价值,
          且不依赖 LLM 主动调工具读取)。
        关闭开关则与旧行为一致:全部完整注入。

        在块顶部加粗体权威性声明,`appearance` 等硬约束 section 前面插入
        「禁止脑补」警告,强化模型对这些字段的遵从度。
        """
        HARD_SECTIONS = {"appearance", "forms"}
        selective = bool(self._cfg("lore_selective_load", True))
        rounds = max(1, int(self._cfg("lore_active_rounds", 6)))
        parts = []

        world_lore = session.get("world_lore") or []
        if world_lore:
            lines = [
                "## 持久化世界观(自动注入每次对话)",
                "**⚠️ 以下世界观设定为本局唯一权威事实,叙事必须严格遵循,严禁凭印象修改、补充或「修正」。**",
            ]
            # 世界观条目通常不多(几个 section 累积),一律**完整注入**,不做按需裁剪:
            # 按需加载依赖 LLM 主动调用读取工具,弱模型可能不读就凭印象写,
            # 直接违反设定权威性;且世界观历史条目的来龙去脉本身就有参考价值。
            # (`lore_selective_load` 只裁剪角色设定段)
            lines.extend(self._render_lore_timeline(world_lore))
            parts.append("\n".join(lines))

        char_lore_dict = self._normalize_character_lore(session.get("character_lore"))
        if any(char_lore_dict.values()):
            active = (
                self._detect_active_characters(session, rounds, current_input)
                if selective
                else None
            )
            # 活跃检测为空时,主角默认视为在场(避免整块只剩摘要提示)
            if (
                selective
                and active is not None
                and not active
                and "主角" in char_lore_dict
            ):
                active = {"主角"}
            lines = [
                "## 持久化角色设定(自动注入每次对话)",
                "**⚠️ 以下角色设定为本局唯一权威事实。描写任何角色前必须先回扫本块,严格按字段值写。**",
                "**外貌(发色/瞳色/发型/服装/配饰/体型等)为硬性约束 — 严禁凭训练印象脑补、换色或「合理化」,除非本块末尾有变更条目明确覆盖。**",
            ]
            if selective:
                lines.append(
                    "**选择性加载:以下仅完整列出最近出场过的角色;未出场角色只显示名字。"
                    '需要刻画未出场角色时,先调 `life_sim_get_character_lore(character="角色名")` '
                    "拿到完整设定再写,不要凭空发挥。**"
                )
            # 按首次出现顺序遍历角色(dict 天然保序),新角色追加在块末尾,
            # 已有角色/条目的字节位置不动 → 前缀缓存不被打断。
            inactive: list[tuple[str, int]] = []
            for char_name in char_lore_dict:
                entries = char_lore_dict[char_name]
                if not entries:
                    continue
                if selective and char_name not in (active or set()):
                    inactive.append((char_name, len(entries)))
                    continue
                lines.append(f"### {char_name}")
                lines.extend(
                    self._render_lore_timeline(
                        entries, indent="- ", hard_sections=HARD_SECTIONS
                    )
                )
            if inactive:
                # 未出场角色合并为紧凑列表(名字 + 条数),提示只写一次
                summary = "、".join(f"{n}({c}条)" for n, c in inactive)
                lines.append("")
                lines.append(f"⏸️ **未出场角色**({len(inactive)} 名):{summary}")
                lines.append(
                    "- 💡 刻画其中任何角色前,调 "
                    '`life_sim_get_character_lore(character="角色名")` '
                    "获取完整设定后再写;忘记角色名时可传空值让工具列出全部。"
                )
            parts.append("\n".join(lines))
        return "\n\n".join(parts)

    @staticmethod
    def _render_lore_timeline(
        entries: list,
        indent: str = "",
        hard_sections: set[str] | None = None,
        max_content_chars: int | None = None,
        max_total_chars: int = 0,
    ) -> list[str]:
        """把 (角色 / 世界观) 的 entries 列表渲染成时间轴字符串列表。

        section 按**首次出现顺序**分组,组内按 seq 升序,每条标注 `[#seq | timestamp]`。
        返回每行已加好 `indent` 前缀的字符串,直接 extend 进块。

        `hard_sections` 指定的 section(如 appearance / forms)在首条前会插入
        一行「禁止脑补」警告,强化模型对这些字段的遵从度。

        `max_content_chars` / `max_total_chars` 用于**用户展示**路径(如 /lore):
        QQ 平台对超长转发消息(>4096 字符)会直接拒绝发送,而角色/世界观条目可能
        单条上千字。设限后每条 content 截断到 `max_content_chars`(默认不截),
        累计超出 `max_total_chars` 时停止追加并提示省略条数。
        LLM 注入 / 工具返回等路径不传这两个参数,保持全文。

        为什么按首次出现顺序而不是字典序:
        - 新 entry 永远是**追加**到对应 section 组末尾,老条目的字节位置不动;
        - 新 section 追加在块末尾 —— 不会像字典序那样插到中间、把整块后续文本
          全部移位,从而保住前缀缓存命中率。
        """
        hard_sections = hard_sections or set()
        # section 首次出现顺序
        section_order: dict[str, int] = {}
        for e in entries:
            sec = str(e.get("section", ""))
            if sec not in section_order:
                section_order[sec] = len(section_order)
        groups: dict[str, list] = {}
        for e in entries:
            groups.setdefault(str(e.get("section", "")), []).append(e)

        lines: list[str] = []
        prev_section: str | None = None
        total_chars = 0
        truncated = False
        for sec in sorted(groups, key=lambda s: section_order.get(s, 0)):
            group = sorted(groups[sec], key=lambda e: int(e.get("seq", 0)))
            for e in group:
                seq = e.get("seq", "?")
                ts = e.get("updated_at", "")
                content = e.get("content", "")
                if (
                    max_content_chars
                    and isinstance(content, str)
                    and len(content) > max_content_chars
                ):
                    content = content[:max_content_chars] + "…"
                line = f"{indent}[#{seq} | {ts}] **{sec}** — {content}"
                if sec != prev_section and sec in hard_sections:
                    warn = (
                        f"{indent}> 🔒 **「{sec}」为硬性约束 — 发色/瞳色/服装/配饰等严禁凭印象脑补,叙事必须照写。**"
                    )
                    if max_total_chars and total_chars + len(warn) > max_total_chars:
                        truncated = True
                        break
                    lines.append(warn)
                    total_chars += len(warn)
                if max_total_chars and total_chars + len(line) > max_total_chars:
                    truncated = True
                    break
                lines.append(line)
                total_chars += len(line)
            if truncated:
                break
            prev_section = sec
        # 截断提示:内容过长时告知总条数与已显示条数
        if truncated:
            all_ = sum(len(g) for g in groups.values())
            lines.append(f"{indent}…(内容过长,已截断,共 {all_} 条,显示 {len(lines)} 条)")
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
            character(string): 角色名,默认="主角"。可用 NPC 真名 / 称号区分。
                角色有固定昵称/简称时,写成 `全名（昵称）` 格式(如 "雪音（小雪）"、
                "汐见花音（花音）"、"梦娜1号（梦娜）"),系统自动提取括号昵称用于
                出场检测;同一角色只建一个 key,不要全名/昵称各存一套。
        Returns:
            确认消息。
        """
        return await self._save_lore(
            event, "character_lore", section, content, character=character
        )

    async def life_sim_get_character_lore(self, event, character: str = "主角") -> str:
        """
        按需读取某个角色的完整持久化设定(剧情需要时调用)

        适用场景:
        - 系统默认**选择性加载**:system prompt 里只完整列出最近出场过的角色,
          未出场角色只显示名字。当某个未出场角色即将登场 / 需要详细刻画时,
          先调本工具拿到它的完整设定(含外貌 / 形态 / 性格 / 关系 / 技能等全部时间轴),再照设定写。
        - 已出场角色的设定可能被选择性加载裁剪时,同样用本工具补全。

        Args:
            character(list[str]): 要读取的角色名,默认="主角",可传字符串数组一次查多个
                (如 ["花音", "银时", "汐见花音"]),也可传单个字符串(如 "花音")。
                每个查询词支持精确名 / 昵称 / 简称;同一个人被拆成多个 key 时会一起返回。
                省略或传空时列出当前已收录角色。
        Returns:
            所有匹配角色的完整设定文本(按 section 分组的时间轴,含 seq 与时间戳);
            多个角色命中时按收录顺序分块返回。
        """
        event_key = self._sim_session_key(event)
        session = await self._load_sim(event)
        if not session:
            return "❌ 当前没有进行中的转生模拟,请先 /创建。"
        # 合并本轮 staging(同轮内可能刚 save 过新条目)
        staging = self._pending_lore.get(event_key) or {}
        char_lore = self._normalize_character_lore(
            staging.get("character_lore") or session.get("character_lore")
        )
        querys = _normalize_character_query(character)
        if not querys:
            candidates = "、".join(c for c in char_lore if char_lore.get(c))
            return f"📋 已收录角色:{candidates or '(暂无)'}。请指定要读取的角色名。"

        # 收集所有查询词的匹配 key(保序去重)
        matched_keys: list[str] = []
        seen: set[str] = set()
        for q in querys:
            for mn in _match_lore_characters(char_lore, q):
                if mn not in seen:
                    seen.add(mn)
                    matched_keys.append(mn)
        if not matched_keys:
            candidates = "、".join(c for c in char_lore if char_lore.get(c))
            return (
                f"❌ 没有匹配到「{'、'.join(querys)}」对应的角色。"
                f"已收录角色:{candidates or '(暂无)'}"
            )

        blocks = []
        for mn in matched_keys:
            entries = char_lore.get(mn) or []
            lines = [f"# 「{mn}」持久化设定(完整时间轴)"]
            lines.append(
                "**⚠️ 以上为唯一权威设定,描写该角色前必须严格按字段值写,禁止脑补。**"
            )
            lines.extend(
                self._render_lore_timeline(
                    entries, hard_sections={"appearance", "forms"}
                )
            )
            blocks.append("\n".join(lines))
        if len(matched_keys) > 1:
            blocks.insert(
                0,
                f"ℹ️ 共匹配到 {len(matched_keys)} 个角色 key(可能含同一角色的不同称呼),以下全部列出:",
            )
        return "\n\n".join(blocks)

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
                - 当前会话的最近 ID 已在当轮用户消息的 <narrative_ref> 标签中给出,直接复制即可
        Returns:
            成功 / 失败消息。
        """
        scope = self._sim_session_key(event)
        branch = ""
        if not narrative or not isinstance(narrative, str):
            return "❌ narrative 不能为空"

        # 解析 record_id:留空 / "last" / "latest" / "prev" 都取最新一条
        # 用 session.last_narrative_id 精准定位 — list() 按 created_at 排序但只到秒,
        # 同秒创建的记录顺序不稳定;session 里的字段由 append 时即时写入,永远指向真正的最后一条
        resolved_id = (record_id or "").strip()
        auto = False
        if not resolved_id or resolved_id.lower() in {
            "last",
            "latest",
            "prev",
            "previous",
        }:
            session = await self._load_sim(event)
            branch = _narrative_branch(session)
            resolved_id = (session or {}).get("last_narrative_id") or ""
            auto = True
            if not resolved_id:
                return "❌ 当前 scope 暂无最近剧情 ID(从未记录过剧情),无法修订"

        # 修订前先抓旧状态,暂存到 staging(供 /undo 回滚;快照本身不存全文,
        # 只有修订发生时才记录 pre-revision 状态,避免每轮重复数据)
        pre_state = None
        existing = await self.narrative_store.get(scope, resolved_id, branch=branch)
        if existing is not None:
            pre_state = {
                "id": resolved_id,
                "narrative": existing.get("narrative", ""),
                "revised_count": int(existing.get("revised_count", 0)),
                "revised_at": existing.get("revised_at", ""),
            }

        ok = await self.narrative_store.revise(scope, resolved_id, narrative, branch=branch)
        if ok:
            # 标记本轮已 revise — 避免 _auto_record_narrative 把修订后的
            # 文本再次当成"新一轮"记录,造成内容几乎相同的重复记录
            if pre_state is not None:
                self._pending_revise.setdefault(scope, []).append(pre_state)
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

    @filter.command("头像", alias={"set_avatar", "设置头像", "头像设置"})
    async def cmd_set_avatar(self, event: AstrMessageEvent):
        """/头像 <角色名> <图片> - 设置角色头像;\n/头像 列表 · /头像 查看 [角色名]"""
        # 提取参数(去掉命令本身)
        arg = self._extract_after_cmd(
            event, ("头像", "set_avatar", "设置头像", "头像设置")
        )
        stripped = arg.strip()
        # 头像按会话 scope 分区(群/私聊彼此隔离)
        scope = self._sim_session_key(event)

        # 列表操作优先(不需要图片)
        if stripped in ("列表", "list", "-l"):
            names = self.avatar_store.list_names(scope)
            if not names:
                yield event.plain_result("📭 还没有设置任何头像")
            else:
                yield event.plain_result(
                    "已设置头像:\n" + "\n".join(f"• {n}" for n in names)
                    + f"\n💡 共 {len(names)} 个, `/头像 查看 <角色名>` 看具体图片"
                )
            return

        # 查看头像(不需要图片): /头像 查看 [角色名]
        if stripped in ("查看",) or stripped.startswith("查看 ") or stripped == "view":
            target = stripped[2:].strip() if stripped.startswith("查看 ") else ""
            if not target:
                # 不带角色名 → 逐个展示全部
                names = self.avatar_store.list_names(scope)
                if not names:
                    yield event.plain_result("📭 还没有设置任何头像")
                    return
                yield event.plain_result(
                    "🖼️ 已设置头像:" + "\n".join(f"• {n}" for n in names)
                )
                for n in names:
                    path = self.avatar_store.get_avatar(n, scope)
                    # 必须复制一份,不能把已存储的头像本身登记为临时文件,
                    # 否则框架会在事件结束后把它删掉(见 _temporary_avatar_copy 注释)。
                    if path:
                        shown = self._temporary_avatar_copy(path, event)
                        if shown:
                            yield event.image_result(shown)
                return
            # 指定角色名 → 只展示该角色
            path = self.avatar_store.resolve(target, scope)
            if not path:
                available = "、".join(self.avatar_store.list_names(scope))
                yield event.plain_result(
                    f"❌ 未找到角色「{target}」的头像。"
                    + (f"\n现有角色: {available}" if available else "")
                    + "\n💡 `/头像 列表` 查看全部"
                )
                return
            shown = self._temporary_avatar_copy(path, event)
            if shown:
                yield event.image_result(shown)
            yield event.plain_result(f"🖼️ 角色「{target}」的头像")
            return

        # 提取图片:当前消息无图时回退到引用消息的图片(手机端常无法同发文字+图)
        imgs = await _extract_image_with_quoted(event)
        if not imgs:
            yield event.plain_result(
                "❌ 请附带一张角色头像图片:\n`/头像 阿龙 <图片>`\n\n也可**引用**(回复)一张图片后跟随该命令。\n查看已设置: `/头像 列表`"
            )
            return
        if not arg:
            yield event.plain_result("❌ 请指定角色名,例如:\n`/头像 阿龙 <图片>`")
            return

        # 取第一张图片
        try:
            img = imgs[0]
            path = await img.convert_to_file_path()
        except Exception as e:
            yield event.plain_result(f"❌ 图片下载失败:{e}")
            return

        name = arg.strip()
        # 重新从文件读字节(convert_to_file_path 已处理网络/本地)
        try:
            data = await asyncio.to_thread(_read_all_bytes, path)
        except Exception as e:
            yield event.plain_result(f"❌ 读取图片失败:{e}")
            return

        saved = self.avatar_store.save_avatar(name, data, scope=scope)
        if saved:
            yield event.plain_result(f"✅ 已为「{name}」设置头像")
        else:
            yield event.plain_result("❌ 保存失败,请检查图片格式 / 角色名")

    @filter.command("删除头像", alias={"del_avatar", "清除头像"})
    async def cmd_del_avatar(self, event: AstrMessageEvent):
        """/删除头像 <角色名> - 删除角色头像"""
        arg = self._extract_after_cmd(event, ("删除头像", "del_avatar", "清除头像"))
        if not arg:
            yield event.plain_result("❌ 用法: `/删除头像 阿龙`")
            return
        names = [n.strip() for n in arg.replace(" ", ",").split(",") if n.strip()]
        if not names:
            yield event.plain_result("❌ 用法: `/删除头像 阿龙`")
            return
        scope = self._sim_session_key(event)
        deleted = []
        for n in names:
            if self.avatar_store.delete(n, scope):
                deleted.append(n)
        if deleted:
            yield event.plain_result(f"🗑️ 已删除: {', '.join(deleted)}")
        else:
            yield event.plain_result("ℹ️ 没有找到对应的头像")

    @filter.command("创建", alias={"create"})
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
        setting = self._extract_after_cmd(event, ("创建", "create"))
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

        n_branches = await self._clear_sim(event)

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
            + (
                f"\n⚠️ 已随旧会话清理 {n_branches} 个剧情分支存档。"
                if n_branches
                else ""
            )
        )
        result = await self._generate(event, session, first_input, mode, imgs)
        async for _ in self._yield_narrative_result(event, result):
            yield _

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
        action = self._extract_after_cmd(event, ("do", "input", "输入"))
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
        async for _ in self._yield_narrative_result(event, result):
            yield _

    @filter.command("进度", alias={"progress"})
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

    @filter.command("删除", alias={"delete", "del"})
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

            n_branches = await self._clear_sim(event)
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
            branch_note = f",{n_branches} 个分支存档" if n_branches else ""
            yield event.plain_result(
                "🗑️ 会话已删除"
                f"{char_note}{sess_note}{branch_note}。\n"
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

    async def _apply_rollback(self, session: dict, scope: str, n: int) -> dict | None:
        """回滚最近 n 轮对话(就地修改 session),供 /undo 与 /redo 共用。

        回滚范围:消息截断 + 持久化 lore / RPG 数值 / 剧情历史 + 各快照数组 + lore_turn。
        不落盘 —— 由调用方决定是否 `_save_sim`(/undo 立即存,/redo 交给 _generate 存)。

        返回展示统计 dict;没有可回滚的 user 消息时返回 None。
        """
        messages = session.get("messages", [])
        # 只统计真实用户轮次:历史压缩产生的摘要消息(_summary)不是一轮 /do,
        # 混入会把 take 数错(进而算错回滚目标轮)。
        user_indices = [
            i
            for i, m in enumerate(messages)
            if m.get("role") == "user" and not m.get("_summary")
        ]
        if not user_indices:
            return None

        take = min(n, len(user_indices))
        cut_idx = user_indices[-take]
        cut_msg = messages[cut_idx]
        removed = messages[cut_idx:]
        messages = messages[:cut_idx]

        # 回滚持久化 lore:用 turn 计数,不受压缩影响
        current_turn = session.get("lore_turn", 0)
        # 目标 turn = 第一个被回滚的 turn 的"开始前"状态。
        # 优先用被截断首条 user 消息上盖的 turn 戳:新会话每轮 /do 都会盖章,
        # 消息与轮次一一对应,即使历史里有失败/空输出轮导致 lore_turn 虚高也不受影响;
        # 老会话(无 turn 戳)用快照指纹推断(见 _legacy_rollback_target_turn),
        # 仍无法推断才按 lore_turn 倒推(旧行为)。
        stamped_turn = cut_msg.get("turn") if isinstance(cut_msg, dict) else None
        if isinstance(stamped_turn, int) and not isinstance(stamped_turn, bool) and stamped_turn >= 1:
            target_turn = stamped_turn
        else:
            target_turn = self._legacy_rollback_target_turn(
                session, take, len(user_indices)
            )
            if target_turn is None:
                target_turn = max(1, current_turn - take + 1)
        snapshots = session.get("lore_snapshots") or []
        target_snapshot = next(
            (s for s in reversed(snapshots) if s["turn"] == target_turn),
            None,
        )
        if target_snapshot:
            (
                session["world_lore"],
                session["character_lore"],
            ) = _resolve_snapshot_lore(session, target_snapshot)
        # 删掉被回滚的快照(turn > target_turn)
        session["lore_snapshots"] = [s for s in snapshots if s["turn"] <= target_turn]
        # 快照去重表同步收敛,丢弃已无快照引用的版本
        _compact_lore_versions(session)
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
            rpg_stats = self._rpg_restore(
                _resolve_rpg_snapshot(session, target_rpg_snap)
            )
        session["rpg_snapshots"] = [
            s for s in rpg_snapshots if s["turn"] <= target_turn
        ]
        # 回滚后收敛去重表,丢弃已无快照引用的版本
        _compact_rpg_versions(session)

        # 回滚剧情历史(新增的删掉、被修订的还原)
        narr_snapshots = session.get("narrative_snapshots") or []
        target_narr_snap = next(
            (s for s in reversed(narr_snapshots) if s.get("turn") == target_turn),
            None,
        )
        narr_stats = None
        if target_narr_snap is not None:
            narr_stats = await self._restore_narrative_history(
                scope,
                target_narr_snap,
                narr_snapshots,
                branch=_narrative_branch(session),
            )
        session["narrative_snapshots"] = [
            s for s in narr_snapshots if s.get("turn", 0) <= target_turn
        ]
        # last_narrative_id 若指向被删除的记录,清空(下次 /do 会重写)
        if narr_stats and narr_stats.get("deleted", 0) > 0:
            branch = _narrative_branch(session)
            remaining = await self.narrative_store.list(scope, branch)
            last_id = remaining[-1]["id"] if remaining else None
            if last_id != session.get("last_narrative_id"):
                session["last_narrative_id"] = last_id

        session["messages"] = messages

        # 统计(给展示用)
        _resolved_lore = (
            _resolve_snapshot_lore(session, target_snapshot)
            if target_snapshot
            else ({}, {})
        )
        char_dict = _resolved_lore[1] or {}
        return {
            "turns": take,
            "removed": removed,
            "user_n": sum(1 for m in removed if m.get("role") == "user"),
            "asst_n": sum(1 for m in removed if m.get("role") == "assistant"),
            "tool_n": sum(1 for m in removed if m.get("role") == "tool"),
            "summary_n": sum(1 for m in removed if m.get("_summary")),
            "lore_restored": lore_restored,
            "lore": {
                "w_n": len((target_snapshot or {}).get("world_lore") or []),
                "c_n": sum(len(v) for v in char_dict.values() if isinstance(v, list)),
                "c_chars": sum(1 for v in char_dict.values() if v),
            },
            "rpg_stats": rpg_stats,
            "narr_stats": narr_stats,
            "remaining_narr": len(
                await self.narrative_store.list(scope, _narrative_branch(session))
            ),
        }

    @staticmethod
    def _legacy_rollback_target_turn(
        session: dict, take: int, user_turns: int
    ) -> int | None:
        """老会话(消息上没有 turn 戳)的回滚目标轮定位:用快照指纹推断。

        背景:旧版在每次 /do 进入 LLM 调用前就递增 lore_turn 并拍快照;
        若当轮调用失败/返回空文本(不落 user 消息、不产生剧情记录),
        lore_turn 会虚高 —— /undo N 按 user 消息数回滚,却按 lore_turn 倒推目标轮,
        导致目标轮偏晚、剧情历史只删了一条。

        原理(不需要消息上的 turn 戳):
        - 每个 /do(成功或失败)都会在 narrative_snapshots 追加一条(轮号=turn);
        - 成功的一轮要么新建了剧情记录(narrative ids / lore / rpg 指纹变化,
          体现在**下一条**快照),要么调用了 revise(本条快照的 `revised` 非空);
        - 失败的一轮两者都不沾 → 快照指纹与上一条完全相同。
        因此「真实轮」= 有指纹变化或有 revised 的快照轮;失败轮不会计入。
        把真实轮按轮号升序与 user 消息一一对应,即可得到每条消息归属的轮号。

        唯一例外:最后一轮若是普通成功轮,它的"新建记录"证据落在不存在的
        下一条快照上 —— 此时真实轮数比消息数少 1,把最后一条快照的轮号补上即可。

        返回:要回滚到的目标轮(即被撤销的第一条 user 消息的轮号);
        无法可靠推断时返回 None(调用方回退到 lore_turn 倒推)。
        """
        narr_snaps = [
            s for s in (session.get("narrative_snapshots") or []) if isinstance(s, dict)
        ]
        if not narr_snaps or take < 1 or user_turns < 1:
            return None
        narr_snaps.sort(key=lambda s: s.get("turn", 0))

        def _j(obj) -> str:
            return json.dumps(obj or [], sort_keys=True, ensure_ascii=False)

        # 按 turn 建 lore / rpg 指纹查找表(新格式为 version 索引,旧格式内联内容)
        lore_by_turn: dict = {}
        for s in session.get("lore_snapshots") or []:
            if not isinstance(s, dict) or not isinstance(s.get("turn"), int):
                continue
            vi = s.get("version")
            lore_by_turn[s["turn"]] = (
                ("v", vi) if isinstance(vi, int)
                else (_j(s.get("world_lore")), _j(s.get("character_lore")))
            )
        rpg_by_turn: dict = {}
        for s in session.get("rpg_snapshots") or []:
            if not isinstance(s, dict) or not isinstance(s.get("turn"), int):
                continue
            vi = s.get("version")
            rpg_by_turn[s["turn"]] = (
                ("v", vi) if isinstance(vi, int)
                else _j({k: s.get(k) for k in ("chars", "sessions") if k in s})
            )

        def _fp(snap: dict):
            t = snap.get("turn")
            return (
                lore_by_turn.get(t),
                _j(snap.get("ids")),
                rpg_by_turn.get(t),
            )

        real_turns: set[int] = set()
        for i, snap in enumerate(narr_snaps):
            t = snap.get("turn")
            if not isinstance(t, int):
                continue
            if snap.get("revised"):
                real_turns.add(t)
            if i + 1 < len(narr_snaps) and _fp(narr_snaps[i + 1]) != _fp(snap):
                # 下一张快照与本章不同 → 本轮新建了记录 / lore / rpg 数据 → 本轮真实
                real_turns.add(t)
        real_list = sorted(real_turns)
        if not real_list:
            return None

        missing = user_turns - len(real_list)
        if missing < 0:
            return None  # 指纹比消息还多(历史被压缩/摘要等异常),放弃推断
        if missing:
            last_turn = narr_snaps[-1].get("turn")
            if not isinstance(last_turn, int):
                return None
            # 最后一轮普通成功轮的"新建记录"证据落在不存在的下一条快照上,
            # 需要按消息数把它补成真实轮(首个缺失名额给最新的轮)。
            real_list = sorted(set(real_list) | {last_turn})
            if len(real_list) != user_turns:
                return None

        if take > len(real_list):
            return None
        k = user_turns - take  # 被撤销的第一条消息在真实轮列表中的下标(0-based)
        if k < 0 or k >= len(real_list):
            return None
        return real_list[k]

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

        scope = self._sim_session_key(event)
        stats = await self._apply_rollback(session, scope, n)
        if stats is None:
            yield event.plain_result("❌ 没有可撤销的轮次")
            return
        await self._save_sim(event, session)

        # 统计
        removed = stats["removed"]
        messages = session.get("messages", [])
        lines = [
            f"⏪ 已撤销最近 {stats['user_n']} 轮对话(删 {len(removed)} 条消息)",
            f"   组成:user × {stats['user_n']}, assistant × {stats['asst_n']}, tool × {stats['tool_n']}"
            + (f", summary × {stats['summary_n']}" if stats["summary_n"] else ""),
            f"   剩余:{len(messages)} 条消息,{stats['remaining_narr']} 条剧情记录",
        ]
        if stats["lore_restored"]:
            w_n = stats["lore"]["w_n"]
            c_n = stats["lore"]["c_n"]
            c_chars = stats["lore"]["c_chars"]
            if w_n or c_n:
                parts = []
                if w_n:
                    parts.append(f"世界观 {w_n} 条")
                if c_n:
                    parts.append(f"角色 {c_n} 条(共 {c_chars} 名)")
                lines.append(f"   📜 持久化设定回滚:{' + '.join(parts)}")
            else:
                lines.append("   📜 持久化设定回滚(本次 turn 无 lore 变更)")
        rpg_stats = stats["rpg_stats"]
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
        narr_stats = stats["narr_stats"]
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

    @filter.command("redo", alias={"重试", "retry"})
    async def cmd_redo(self, event: AstrMessageEvent):
        """/redo - 重试上一轮:回滚最近一轮并用相同输入重新生成(不必手动 /undo + /do)"""
        lock = self._get_sim_lock(self._sim_session_key(event))
        if lock.locked():
            yield event.plain_result(self._busy_message())
            return

        async with lock:
            async for _ in self._cmd_redo_body(event):
                yield _

    async def _cmd_redo_body(self, event: AstrMessageEvent):
        session = await self._load_sim(event)
        if not session:
            yield event.plain_result(
                "❌ 当前没有进行中的转生模拟,请先使用 /创建 <世界观> 开始。"
            )
            return

        messages = session.get("messages", [])
        # 找最后一个真实的 user 输入(跳过历史压缩生成的摘要消息)
        last_user = next(
            (
                m
                for m in reversed(messages)
                if m.get("role") == "user" and not m.get("_summary")
            ),
            None,
        )
        if last_user is None:
            yield event.plain_result("❌ 没有可重试的轮次(至少需要一轮 /do)。")
            return

        # 回滚前先提取上一轮的原始输入:文本(剥系统标签、保留引用)+ 图片
        content = last_user.get("content")
        user_input = _strip_meta_tags(_content_to_text(content))
        if not user_input:
            user_input = "请继续推进剧情(重新生成上一轮输出)"
        imgs = _restore_images_from_content(content)

        scope = self._sim_session_key(event)
        stats = await self._apply_rollback(session, scope, 1)
        if stats is None:
            yield event.plain_result("❌ 没有可重试的轮次")
            return
        # 注意:回滚后不立即落盘 —— _generate 成功时会统一 save;
        # 若生成失败,磁盘仍保留上一轮原样,可继续 /redo。

        mode = session.get("mode", "A")
        yield event.plain_result(
            f"🔄 正在重新生成上一轮 [模式 {mode}]"
            + ("(含图片)" if imgs else "")
            + "..."
        )
        result = await self._generate(event, session, user_input, mode, imgs)
        async for _ in self._yield_narrative_result(event, result):
            yield _

    # ════════════════════════════════════════════════════════════════
    # 剧情历史:列表 / 上传 / 删除
    # ════════════════════════════════════════════════════════════════

    @filter.command("历史", alias={"history"})
    async def cmd_history(self, event: AstrMessageEvent):
        """/历史 [N] - 列出当前会话最近的 N 条剧情记录(默认 10)"""
        arg = self._extract_after_cmd(event, ("历史", "history")).strip()
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
        session = await self._load_sim(event)
        records = await self.narrative_store.list(
            scope, _narrative_branch(session)
        )
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

    @filter.command("上传历史", alias={"upload_history"})
    async def cmd_upload_history(self, event: AstrMessageEvent):
        """/上传历史 [jsonl] [last N|all] - 把剧情历史导出为文件并发送。
        默认导出当前 scope 全部记录,JSON 格式(含世界设定/角色设定快照)。
        jsonl:每条记录一行,便于分批读取
        last N:仅导出最近 N 条
        all:导出所有 scope 的记录(本用户/本群能访问到的全部)
        """
        arg = (
            self._extract_after_cmd(event, ("上传历史", "upload_history"))
            .strip()
            .lower()
        )
        use_jsonl = "jsonl" in arg
        scope = self._sim_session_key(event)
        session = await self._load_sim(event)

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
            records = await self.narrative_store.list(
                scope, _narrative_branch(session)
            )
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
        def _write_export() -> None:
            # 阻塞文件 IO 放到线程池,避免卡住事件循环
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
                    f.writelines(
                        json.dumps(r, ensure_ascii=False) + "\n" for r in records
                    )
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

        try:
            await asyncio.to_thread(_write_export)
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

    @filter.command("删除历史", alias={"delete_history"})
    async def cmd_delete_history(self, event: AstrMessageEvent):
        """/删除历史 <id|all> - 删除指定剧情记录,或 all 删除当前 scope 全部"""
        arg = self._extract_after_cmd(event, ("删除历史", "delete_history")).strip()
        if not arg:
            yield event.plain_result(
                "❌ 用法:\n"
                "  `/删除历史 <id>`  — 删除指定 ID(如 `n_a1b2c3d4`)\n"
                "  `/删除历史 all`  — 删除当前 scope 全部记录"
            )
            return

        scope = self._sim_session_key(event)
        session = await self._load_sim(event)
        branch = _narrative_branch(session)

        if arg.lower() in ("all", "全部"):
            n = await self.narrative_store.delete_scope(scope)
            yield event.plain_result(f"🗑️ 已清空 scope=`{scope}` 全部剧情记录({n} 条)")
            return

        # 单条删除
        target = arg.split()[0].strip()
        if not target:
            yield event.plain_result("❌ 请提供记录 ID")
            return
        ok = await self.narrative_store.delete(scope, target, branch=branch)
        if ok:
            yield event.plain_result(f"🗑️ 已删除剧情记录 `{target}`")
        else:
            yield event.plain_result(
                f"❌ 找不到记录 `{target}`(可能 ID 输错,或不在当前 scope)"
            )

    # ════════════════════════════════════════════════════════════════
    # 剧情分支:保存 / 切换 / 列表 / 删除
    # ════════════════════════════════════════════════════════════════

    @staticmethod
    def _parse_branch_name_desc(rest: str) -> tuple[str, str]:
        """把 `/分支 保存 <名称> [说明]` 的剩余参数拆成 (名称, 说明)。"""
        rest = (rest or "").strip()
        if not rest:
            return "", ""
        name, _, desc = rest.partition(" ")
        name = name.strip()
        return name[:30], desc.strip()

    async def _branch_capture(self, session: dict, event) -> dict:
        """把当前会话状态完整快照为一个分支(含消息 / lore / RPG / 剧情历史)。

        快照由调用方写入 BranchStore(独立于会话存储),切换分支时用
        `_branch_restore` 整体还原。
        刻意**不包含**分支列表 / current_branch — 它们属于会话运行时状态,
        分支快照只保存"从这一刻往后继续推进所需的全部状态"。
        """
        mode = session.get("mode", "A")
        return {
            "world_setting": session.get("world_setting"),
            "mode": mode,
            "owner_id": session.get("owner_id"),
            "owner_name": session.get("owner_name"),
            "created_at": session.get("created_at"),
            "messages": copy.deepcopy(session.get("messages", [])),
            "world_lore": copy.deepcopy(session.get("world_lore") or []),
            "character_lore": copy.deepcopy(
                self._normalize_character_lore(session.get("character_lore"))
            ),
            "last_narrative_id": session.get("last_narrative_id"),
            "lore_turn": session.get("lore_turn", 0),
            "lore_snapshots": copy.deepcopy(session.get("lore_snapshots") or []),
            "_lore_versions": copy.deepcopy(session.get("_lore_versions") or []),
            "rpg_snapshots": copy.deepcopy(session.get("rpg_snapshots") or []),
            "_rpg_versions": copy.deepcopy(session.get("_rpg_versions") or []),
            "narrative_snapshots": copy.deepcopy(
                session.get("narrative_snapshots") or []
            ),
            # RPG 存档/会话的磁盘快照 — 还原时整体回滚
            # 剧情历史不再内嵌:切换分支时用 `narrative_store.switch_to_branch`
            # 直接复用同目录的分支历史文件(branch_<名>.json)
            "rpg_state": self._rpg_snapshot(event, mode),
        }

    async def _branch_restore(self, branch: dict, event, branch_name: str = "") -> dict:
        """把会话还原到分支保存时的状态,返回新 session dict。

        剧情历史**不在这里复制** — 新设计里主线 = history.json、分支 =
        branch_<名>.json 都是独立文件,切换只是把 `current_branch` 设为
        branch_name,后续读写自动落到对应文件。分支历史文件缺失时,调用方
        先用分支内嵌记录(旧格式 narrative_records)补齐,再走这里。

        current_branch 由调用方在还原后设置,不在本函数内处理。
        """
        scope = self._sim_session_key(event)
        new_session = {
            "world_setting": branch.get("world_setting"),
            "mode": branch.get("mode", "A"),
            "owner_id": branch.get("owner_id"),
            "owner_name": branch.get("owner_name"),
            "created_at": branch.get("created_at"),
            "messages": copy.deepcopy(branch.get("messages") or []),
            "world_lore": copy.deepcopy(branch.get("world_lore") or []),
            "character_lore": copy.deepcopy(branch.get("character_lore") or {}),
            "last_narrative_id": branch.get("last_narrative_id"),
            "lore_turn": branch.get("lore_turn", 0),
            "lore_snapshots": copy.deepcopy(branch.get("lore_snapshots") or []),
            "_lore_versions": copy.deepcopy(branch.get("_lore_versions") or []),
            "rpg_snapshots": copy.deepcopy(branch.get("rpg_snapshots") or []),
            "_rpg_versions": copy.deepcopy(branch.get("_rpg_versions") or []),
            "narrative_snapshots": copy.deepcopy(
                branch.get("narrative_snapshots") or []
            ),
        }
        # RPG 数值回滚到分支点(含新建角色/会话的清理)
        rpg_state = branch.get("rpg_state")
        if rpg_state:
            self._rpg_restore(rpg_state)
        # 剧情历史:优先直接复制同目录的分支历史文件(快,且 versions 自洽);
        # 旧分支文件里内嵌的 narrative_records 则回退整体重建。
        switched = False
        if branch_name:
            switched = await self.narrative_store.switch_to_branch(scope, branch_name)
        if not switched:
            records = branch.get("narrative_records") or []
            if records:
                await self.narrative_store.overwrite_all(scope, records)
        return new_session

    @filter.command("分支", alias={"branch"})
    async def cmd_branch(self, event: AstrMessageEvent):
        """/分支 [保存|切换|列表|删除] - 剧情分支管理(TE/BE/HE 多结局存档)"""
        lock = self._get_sim_lock(self._sim_session_key(event))
        if lock.locked():
            yield event.plain_result(self._busy_message())
            return

        async with lock:
            async for _ in self._cmd_branch_body(event):
                yield _

    async def _cmd_branch_body(self, event: AstrMessageEvent):
        arg = self._extract_after_cmd(event, ("分支", "branch")).strip()
        session = await self._load_sim(event)
        if not session:
            yield event.plain_result(
                "❌ 当前没有进行中的转生模拟,请先使用 /创建 <世界观> 开始。"
            )
            return

        scope = self._sim_session_key(event)

        # 迁移旧数据:老版本把分支快照整体塞在 session["branches"] 里,随会话文件读写。
        # 升级到独立存储后,首次进入分支命令时把它们搬到 BranchStore,并从会话中移除,
        # 避免每次 /do 都带着整个分支树写盘(快照可能很大)。
        legacy = session.pop("branches", None)
        if isinstance(legacy, dict):
            migrated = 0
            for name, b in legacy.items():
                if name and isinstance(b, dict):
                    await self.branch_store.save(scope, name, b)
                    migrated += 1
            await self._save_sim(event, session)
            logger.info(f"life-sim: 已迁移 {migrated} 个旧分支到独立存储 scope={scope}")

        parts = arg.split(maxsplit=1)
        sub = (parts[0] if parts else "").lower()
        rest = parts[1] if len(parts) > 1 else ""
        branches = await self.branch_store.list(scope)

        # ── 列表(默认) ──
        if not sub or sub in ("列表", "list", "ls", "查看"):
            current = session.get("current_branch")
            lines = ["🌿 剧情分支管理"]
            if not branches:
                lines.append("  (暂无分支 — 当前进度尚未保存为分支)")
            else:
                for name, b in branches.items():
                    turn = b.get("lore_turn", 0)
                    label = (b.get("label") or "").strip()
                    tag = "  [自动]" if name == "主线" else ""
                    desc = f" — {label}" if label else ""
                    marker = "  ← 当前" if name == current else ""
                    lines.append(f"  · {name}(第 {turn} 轮){tag}{desc}{marker}")
            lines += [
                "",
                "用法:",
                "  /分支 当前 — 查看当前所在分支与进度",
                "  /分支 保存 <名称> [说明] — 保存当前进度为分支(如 TE线 / BE线)",
                "  /分支 切换 <名称> — 回到该分支(切换前的主线自动保留为「主线」)",
                "  /分支 删除 <名称> — 删除分支",
            ]
            yield event.plain_result("\n".join(lines))
            return

        # ── 当前 ──
        if sub in ("当前", "now", "current", "info", "详情"):
            name = session.get("current_branch")
            turn_now = session.get("lore_turn", 0)
            n_records = len(
                await self.narrative_store.list(scope, _narrative_branch(session))
            )
            position = ""
            for m in reversed(session.get("messages") or []):
                if m.get("role") == "assistant":
                    for ln in _content_to_text(m.get("content")).split("\n"):
                        s = ln.strip()
                        if s.startswith(("## ", "# ")):
                            position = s.lstrip("# ").strip()[:40]
                            break
                    if position:
                        break
            lines = ["🌿 当前分支"]
            if name and name in branches:
                b = branches[name]
                b_turn = b.get("lore_turn", 0)
                label = (b.get("label") or "").strip()
                lines.append(f"  · {name}")
                if label:
                    lines.append(f"    📍 说明:{label}")
                lines.append(f"    📏 分支存档点:第 {b_turn} 轮")
                if turn_now > b_turn:
                    lines.append(
                        f"    🎯 当前进度:第 {turn_now} 轮(已从分支点推进 {turn_now - b_turn} 轮)"
                    )
                else:
                    lines.append(f"    🎯 当前进度:第 {turn_now} 轮(正好在分支点)")
            elif name:
                lines.append(f"  · {name}(该分支已被删除,现在处于其延续线上)")
                lines.append(f"    🎯 当前进度:第 {turn_now} 轮")
            else:
                lines.append("  · 主线(进行中,尚未从任何保存的分支继续)")
                lines.append(f"    🎯 当前进度:第 {turn_now} 轮")
            if position:
                lines.append(f"    📌 当前位置:{position}")
            lines.append(f"    📝 剧情记录:{n_records} 条")
            lines.append("    💡 /分支 列表 查看全部分支 · /分支 切换 <名称> 切换路线")
            yield event.plain_result("\n".join(lines))
            return

        # ── 保存 ──
        if sub in ("保存", "save", "set", "存档"):
            name, desc = self._parse_branch_name_desc(rest)
            if not name:
                yield event.plain_result(
                    "❌ 用法:`/分支 保存 <名称> [说明]`(如 `/分支 保存 TE线 王都线结局`)"
                )
                return
            if name == "主线":
                yield event.plain_result("❌ 「主线」是自动保留分支名,请换一个名字。")
                return
            capture = await self._branch_capture(session, event)
            capture["label"] = desc
            overwritten = name in branches
            await self.branch_store.save(scope, name, capture)
            # 剧情历史同目录归档一份(切换时直接复制该文件,不重建)
            await self.narrative_store.save_branch_history(scope, name)
            turn = capture.get("lore_turn", 0)
            overwrite_note = "(已覆盖同名分支)" if overwritten else ""
            yield event.plain_result(
                f"🌿 分支「{name}」已保存 {overwrite_note}\n"
                f"   位置:第 {turn} 轮"
                + (f" | 说明: {desc}" if desc else "")
                + "\n💡 继续推进后随时可用 /分支 切换 回到这里体验另一条路线。"
            )
            return

        # ── 切换 ──
        if sub in ("切换", "switch", "to", "回", "load"):
            name = rest.strip()
            if not name:
                yield event.plain_result(
                    "❌ 用法:`/分支 切换 <名称>`(先 /分支 列表 查看)"
                )
                return
            if name not in branches:
                yield event.plain_result(
                    f"❌ 分支「{name}」不存在。用 /分支 列表 查看已有分支。"
                )
                return
            # 新设计:主线 = history.json 恒在;分支 = branch_<名>.json 独立文件。
            # 切换分支只改会话 current_branch 字段,历史读写自动落到对应文件,零复制。
            # 兼容老分支(历史内嵌在分支快照 narrative_records):若分支历史文件不存在,
            # 先用内嵌记录补齐一份。
            target = await self.branch_store.get(scope, name)
            if not target:
                yield event.plain_result(f"❌ 分支「{name}」数据读取失败,请重试。")
                return
            if not await self.narrative_store.branch_exists(scope, name):
                legacy_records = target.get("narrative_records") or []
                if legacy_records:
                    await self.narrative_store.overwrite_all(
                        scope, legacy_records, branch=name
                    )

            new_session = await self._branch_restore(target, event, branch_name=name)
            new_session["current_branch"] = name  # 标记当前所在分支
            await self._save_sim(event, new_session)

            # 回显目标分支最后一段剧情位置
            position = ""
            for m in reversed(target.get("messages") or []):
                if m.get("role") == "assistant":
                    for ln in _content_to_text(m.get("content")).split("\n"):
                        s = ln.strip()
                        if s.startswith(("## ", "# ")):
                            position = s.lstrip("# ").strip()[:40]
                            break
                    if position:
                        break
            lines = [f"🌿 已切换到分支「{name}」(第 {target.get('lore_turn', 0)} 轮)"]
            if position:
                lines.append(f"   📍 当前进度:{position}")
            lines.append("   直接 /do 继续推进即可。")
            yield event.plain_result("\n".join(lines))
            return

        # ── 删除 ──
        if sub in ("删除", "del", "delete", "remove"):
            name = rest.strip()
            if not name:
                yield event.plain_result("❌ 用法:`/分支 删除 <名称>`")
                return
            if name == "主线":
                yield event.plain_result(
                    "❌ 「主线」是自动保留分支,不能用此命令删除(它每次切换前自动更新)。"
                )
                return
            if name not in branches:
                yield event.plain_result(f"❌ 分支「{name}」不存在。")
                return
            await self.branch_store.delete(scope, name)
            # 同目录的分支历史文件一并清理
            await self.narrative_store.delete_branch_history(scope, name)
            # 删除的正是当前分支时,回到「主线」标记(仅此时才需要落盘会话)
            if session.get("current_branch") == name:
                session["current_branch"] = None
                await self._save_sim(event, session)
            yield event.plain_result(f"🗑️ 分支「{name}」已删除。")
            return

        yield event.plain_result(
            "❌ 未知子命令。支持:当前 / 保存 / 切换 / 列表 / 删除\n"
            "   例:/分支 当前 · /分支 保存 TE线 结局线 · /分支 切换 TE线 · /分支 列表"
        )

    # ════════════════════════════════════════════════════════════════
    # 角色 lore 查看
    # ════════════════════════════════════════════════════════════════

    @filter.command("lore", alias={"设定", "角色设定", "人物设定"})
    async def cmd_lore(self, event: AstrMessageEvent):
        """/lore [角色名|世界观] - 查看角色/世界观持久化设定

        /lore 删除 <角色名> - 删除指定角色的当前持久化设定(不动快照,/undo 可恢复)
        """
        session = await self._load_sim(event)
        if not session:
            yield event.plain_result(
                "❌ 当前没有进行中的转生模拟,请先使用 /创建 <世界观> 开始。"
            )
            return

        arg = self._extract_after_cmd(
            event, ("lore", "设定", "角色设定", "人物设定")
        ).strip()
        char_lore = self._normalize_character_lore(session.get("character_lore"))
        world_lore = session.get("world_lore") or []

        # ── 删除角色 ──
        tokens = arg.split(None, 1)
        if tokens and tokens[0] in ("删除", "del", "delete", "remove"):
            target = (tokens[1] if len(tokens) > 1 else "").strip()
            if not target:
                yield event.plain_result("❌ 用法:`/lore 删除 <角色名>`")
                return
            matched_keys = _match_lore_characters(char_lore, target)
            if not matched_keys:
                available = "、".join(n for n in char_lore if char_lore[n])
                yield event.plain_result(
                    f"❌ 未找到角色「{target}」。"
                    + (f"现有角色:{available}" if available else "暂无角色设定")
                    + "\n💡 /lore 查看总览 · /lore 删除 <角色名>"
                )
                return
            removed = {mn: len(char_lore.pop(mn) or []) for mn in matched_keys}
            session["character_lore"] = char_lore
            await self._save_sim(event, session)
            total = sum(removed.values())
            lines = [f"🗑️ 已删除 {len(matched_keys)} 个角色 key、共 {total} 条设定:"]
            for mn in matched_keys:
                lines.append(f"  👤 {mn}: {removed.get(mn, 0)} 条")
            lines.append("   💡 lore 快照未动,误删可用 /undo 回滚恢复。")
            yield event.plain_result("\n".join(lines))
            return

        # ── 总览 ──
        if not arg:
            lines = ["📖 当前设定总览"]
            if world_lore:
                lines.append(f"  🌍 世界观设定: {len(world_lore)} 条")
            chars = [(n, es) for n, es in char_lore.items() if es]
            if chars:
                for n, es in chars:
                    lines.append(f"  👤 {n}: {len(es)} 条")
            else:
                lines.append(
                    "  👤 暂无角色设定(life_sim_save_character_lore 后自动累积)"
                )
            lines += [
                "",
                "💡 /lore <角色名> 查看该角色全部设定 · /lore 世界观 查看世界设定",
                "💡 /lore 删除 <角色名> 删除该角色全部设定",
            ]
            yield event.plain_result("\n".join(lines))
            return

        # ── 世界观 ──
        if arg in ("世界观", "world", "world_lore"):
            if not world_lore:
                yield event.plain_result("🌍 暂无世界观设定。")
                return
            lines = ["🌍 持久化世界观(时间轴):"]
            lines.extend(
                self._render_lore_timeline(
                    world_lore,
                    indent="  ",
                    max_content_chars=300,
                    max_total_chars=3500,
                )
            )
            yield event.plain_result("\n".join(lines))
            return

        # ── 角色名匹配:复用 _match_lore_characters(精确 / 括号别名 / 昵称 / 简称 / 互相包含)──
        target = arg.strip()
        matched_keys = _match_lore_characters(char_lore, target)
        if not matched_keys:
            available = "、".join(n for n in char_lore if char_lore[n])
            yield event.plain_result(
                f"❌ 未找到角色「{target}」。"
                + (f"现有角色:{available}" if available else "暂无角色设定")
                + "\n💡 /lore 查看总览 · /lore 世界观 查看世界设定"
            )
            return

        if len(matched_keys) > 1:
            yield event.plain_result(
                f"📎 匹配到 {len(matched_keys)} 个角色 key(可能为同一角色不同称呼),以下全部列出:"
            )
        for mn in matched_keys:
            entries = char_lore[mn] or []
            if not entries:
                yield event.plain_result(f"👤 {mn}:暂无设定条目。")
                continue
            lines = [f"👤 {mn} 设定(时间轴):"]
            lines.extend(
                self._render_lore_timeline(
                    entries,
                    indent="  ",
                    hard_sections={"appearance", "forms"},
                    max_content_chars=300,
                    max_total_chars=3500,
                )
            )
            yield event.plain_result("\n".join(lines))

    async def terminate(self):
        logger.info("life-sim: 插件已卸载")
