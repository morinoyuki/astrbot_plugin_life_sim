"""Markdown → 中间表示。

纯解析,无绘制逻辑。把 LLM 输出的 markdown 文本解析为块(Block)列表,
每个块内含富文本段(Span)。支持:

块级:标题 / 段落 / 对白 / 列表 / 引用 / 代码块 / 表格 / 水平线 / 图片
行内:粗体 ** ** / 斜体 * * / 删除线 ~~ ~~ / 行内代码 ` ` / 链接 [t](u)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

__all__ = [
    "Span",
    "Block",
    "parse_blocks",
    "plain_text",
]

# ═════════════════════════════════════════════════════════════════════
# 数据结构
# ═════════════════════════════════════════════════════════════════════


@dataclass
class Span:
    """富文本片段。"""

    text: str
    bold: bool = False
    italic: bool = False
    strike: bool = False
    code: bool = False
    link: str = ""


@dataclass
class Block:
    """块基类。"""

    type: str = "paragraph"
    spans: List[Span] = field(default_factory=list)


@dataclass
class Heading(Block):
    def __init__(self, level: int, spans: List[Span]):
        super().__init__("heading", spans)
        self.level = level


@dataclass
class Dialogue(Block):
    """对白块。"""

    def __init__(
        self,
        speaker: str,
        content: str,
        protagonist: bool = False,
        avatar: Optional[str] = None,
    ):
        super().__init__("dialogue")
        self.speaker = speaker.strip()
        self.protagonist = bool(protagonist)
        # LLM 可通过「角色名@头像名」显式指定该气泡使用哪张已有头像(默认按角色名匹配)
        self.avatar = (avatar or "").strip() or None
        self.spans = parse_inline(content.strip())


@dataclass
class ListBlock(Block):
    def __init__(self, ordered: bool):
        super().__init__("list")
        self.ordered = ordered
        self.items: List[List[Span]] = []


@dataclass
class Quote(Block):
    def __init__(self, spans: List[Span]):
        super().__init__("quote", spans)


@dataclass
class CodeBlock(Block):
    def __init__(self, code: str, lang: str = ""):
        super().__init__("code")
        self.code = code
        self.lang = lang


@dataclass
class TableBlock(Block):
    def __init__(self):
        super().__init__("table")
        self.header: List[str] = []
        self.rows: List[List[str]] = []


@dataclass
class HR(Block):
    def __init__(self):
        super().__init__("hr")


@dataclass
class ImageBlock(Block):
    def __init__(self, alt: str, url: str):
        super().__init__("image")
        self.alt = alt
        self.url = url


def plain_text(spans: List[Span]) -> str:
    return "".join(s.text for s in spans)


# ═════════════════════════════════════════════════════════════════════
# 行内解析
# ═════════════════════════════════════════════════════════════════════

_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_CODE_RE = re.compile(r"`([^`\n]+)`")
_STYLE_RE = re.compile(
    r"(\*\*[^*\n]+\*\*"
    r"|~~[^~\n]+~~"
    r"|\*[^*\n]+\*"
    r")"
)


def parse_inline(text: str) -> List[Span]:
    """把纯文本解析为 span 列表。"""
    if not text:
        return []
    spans: List[Span] = []

    # 1. 提取行内代码
    tmp: List[Span] = []
    for idx, part in enumerate(_CODE_RE.split(text)):
        if not part:
            continue
        if idx % 2 == 1:
            tmp.append(Span(text=part, code=True))
        else:
            tmp.extend(_parse_style(part))

    # 2. 链接 优先于纯文本
    spans = parse_links(tmp)
    return _merge(spans)


def _parse_style(text: str) -> List[Span]:
    """解析单个片段内的粗体/斜体/删除线。"""
    if not text:
        return []
    out: List[Span] = []
    for seg in _STYLE_RE.split(text):
        if not seg:
            continue
        if seg.startswith("**") and seg.endswith("**") and len(seg) > 4:
            out.append(Span(text=seg[2:-2], bold=True))
        elif seg.startswith("~~") and seg.endswith("~~") and len(seg) > 4:
            out.append(Span(text=seg[2:-2], strike=True))
        elif (
            seg.startswith("*")
            and seg.endswith("*")
            and len(seg) > 2
            and not seg.startswith("**")
        ):
            out.append(Span(text=seg[1:-1], italic=True))
        else:
            out.append(Span(text=seg))
    return out


def parse_links(spans: List[Span]) -> List[Span]:
    """从 spans 中提取链接。"""
    out: List[Span] = []
    for sp in spans:
        if sp.link or sp.code or not sp.text:
            out.append(sp)
            continue
        text = sp.text
        last = 0
        for m in _LINK_RE.finditer(text):
            if m.start() > last:
                out.append(Span(text=text[last : m.start()], bold=sp.bold))
            out.append(Span(text=m.group(1), link=m.group(2), bold=sp.bold))
            last = m.end()
        if last < len(text):
            out.append(Span(text=text[last:], bold=sp.bold))
        elif last == 0:
            out.append(sp)
    return out


def _merge(spans: List[Span]) -> List[Span]:
    """合并相同样式的相邻片段。"""
    if not spans:
        return []
    out: List[Span] = [spans[0]]
    for s in spans[1:]:
        last = out[-1]
        if (
            last.bold == s.bold
            and last.italic == s.italic
            and last.strike == s.strike
            and last.code == s.code
            and last.link == s.link
        ):
            last.text += s.text
        else:
            out.append(s)
    return out


# ═════════════════════════════════════════════════════════════════════
# 块级解析
# ═════════════════════════════════════════════════════════════════════

_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.*)$")
_FENCE_OPEN = re.compile(r"^ {0,3}(```|~~~)(\w*)\s*$")
_FENCE_CLOSE = re.compile(r"^ {0,3}(```|~~~)\s*$")
_HR_RE = re.compile(r"^ {0,3}([-*_]) ?(\1 ?){2,}$")
_UL_RE = re.compile(r"^ {0,3}[-*+]\s+(.*)$")
_OL_RE = re.compile(r"^ {0,3}\d+[.)、]\s+(.*)$")
_QUOTE_RE = re.compile(r"^ {0,3}>\s?(.*)$")
_IMG_LINE_RE = re.compile(r"^!\[([^\]]*)\]\(([^)\s]+)\)\s*$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")

# 角色名对白:名字(不含标点) + 冒号 + 内容
# 主角标记:名字前加 `*`(由 LLM 按剧情判断谁是主角)
_DIALOGUE_RE = re.compile(r"^(.{1,24}?)[:：]\s*(.+)$", re.S)
_DIALOGUE_BAD_PREFIX = ("http", "https", "www.")


def _looks_like_dialogue(
    line: str,
) -> Optional[tuple[str, str, bool, Optional[str]]]:
    """如果一行像 `角色名: 对白`,返回 (角色名, 内容, 是否主角, 头像覆盖)。

    主角标记:``*角色名: 台词`` —— 名字前加一个星号,渲染时靠右显示蓝色气泡。
    每个阶段的主角由 LLM 根据剧情判断,不硬编码名字。

    头像覆盖:``角色名@头像名: 台词`` —— 说话人仍是 `角色名`,但气泡强制借用
    `头像名` 那张已有头像(默认按角色名匹配)。头像名不存在时不指定。
    """
    line = line.rstrip("\n")
    stripped = line.strip()
    if not stripped:
        return None
    # 排除以 http 开头的
    low = stripped.lower()
    if any(low.startswith(p) for p in _DIALOGUE_BAD_PREFIX):
        return None
    m = _DIALOGUE_RE.match(stripped)
    if not m:
        return None
    who_raw = m.group(1).strip()
    content = m.group(2).strip()
    if not who_raw or not content:
        return None

    # 排除系统/元信息行:剧情ID标记、emoji标记行、方括号开头的标签
    if (
        stripped.startswith("📝")
        or stripped.startswith("[")
        or "剧情ID" in stripped
        or "narrative_ref" in stripped.lower()
        or repr(who_raw).startswith("'📝")
    ):
        return None

    protagonist = False
    who = who_raw
    if who_raw.startswith("*") and len(who_raw) > 1:
        protagonist = True
        who = who_raw[1:].strip()

    # 头像覆盖标记:「角色名@头像名」→ 该气泡借用另一张已有头像(默认按角色名匹配)
    # 例: 阿龙@汐见小亚: ... → 说话人显示「阿龙」,但气泡用「汐见小亚」的头像
    avatar_key = None
    if "@" in who:
        base, _, av = who.partition("@")
        base = base.strip()
        avatar_key = av.strip()
        if not base:
            return None
        who = base

    # 角色名不允许包含超长 / 标点
    if not who or len(who) > 12:
        return None
    # 角色名不能包含明显不是名字的字符,排除含方括号/反引号的系统标签
    if any(c in who for c in "[]`"):
        return None
    # 角色名不能包含明显不是名字的字符
    if " " in who and not who.replace(" ", "").isalnum():
        # 允许"林 晓","Mr. Smith"等
        if not all(part.strip() for part in who.split()):
            return None
    if "\n" in who or "  " in who:
        return None
    return who, content, protagonist, avatar_key


def _strip_quotes(text: str) -> str:
    pairs = [("「", "」"), ("『", "』"), ("“", "”"), ("\"", "\""), ("'", "'")]
    for op, cl in pairs:
        if len(text) >= 2 and text[0] == op and text[-1] == cl:
            return text[1:-1]
    return text


def parse_blocks(text: str) -> List[Block]:
    blocks: List[Block] = []
    lines = (text or "").replace("\r\n", "\n").split("\n")

    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # 空白行
        if not stripped:
            i += 1
            continue

        # 标题
        m = _HEADING_RE.match(line)
        if m:
            # 计算 level 并去掉原有 # 的影响
            level = min(len(m.group(1)), 6)
            body = m.group(2).strip()
            blocks.append(Heading(level, parse_inline(body)))
            i += 1
            continue

        # 代码块
        m = _FENCE_OPEN.match(line)
        if m:
            lang = m.group(2) or ""
            i += 1
            code_lines = []
            while i < n and not _FENCE_CLOSE.match(lines[i]):
                code_lines.append(lines[i])
                i += 1
            if i < n:  # 跳过闭合
                i += 1
            else:  # 未闭合,保留后续所有行
                pass
            blocks.append(CodeBlock("\n".join(code_lines), lang))
            continue

        # 水平线
        if _HR_RE.match(line) and len(stripped) >= 3:
            blocks.append(HR())
            i += 1
            continue

        # 图片单行
        m = _IMG_LINE_RE.match(line)
        if m:
            blocks.append(ImageBlock(m.group(1), m.group(2)))
            i += 1
            continue

        # 对白(需在段落之前)
        if i == n - 1 or not lines[i + 1].strip() or _DIALOGUE_RE.match(stripped):
            diag = _looks_like_dialogue(line)
            if diag:
                who, content, protagonist, avatar_key = diag
                blocks.append(
                    Dialogue(who, _strip_quotes(content), protagonist, avatar=avatar_key)
                )
                i += 1
                continue

        # 无序列表(连续收集)
        if _UL_RE.match(line):
            lb = ListBlock(ordered=False)
            while i < n:
                m_ul = _UL_RE.match(lines[i])
                if not m_ul:
                    break
                lb.items.append(parse_inline(m_ul.group(1).strip()))
                i += 1
            blocks.append(lb)
            continue

        # 有序列表
        if _OL_RE.match(line):
            lb = ListBlock(ordered=True)
            while i < n:
                m_ol = _OL_RE.match(lines[i])
                if not m_ol:
                    break
                lb.items.append(parse_inline(m_ol.group(1).strip()))
                i += 1
            blocks.append(lb)
            continue

        # 引用
        if _QUOTE_RE.match(line):
            quote_spans: List[Span] = []
            while i < n:
                mq = _QUOTE_RE.match(lines[i])
                if not mq:
                    break
                if quote_spans:
                    quote_spans.append(Span(text=" "))
                quote_spans.extend(parse_inline(mq.group(1)))
                i += 1
            blocks.append(Quote(quote_spans))
            continue

        # 表格
        if _TABLE_ROW_RE.match(line):
            try:
                header = _split_table_row(line)
                if header and len(header) >= 1 and i + 1 < n:
                    sep = lines[i + 1].strip()
                    if _TABLE_SEP_RE.match(sep):
                        tb = TableBlock()
                        tb.header = header
                        i += 2
                        while i < n and _TABLE_ROW_RE.match(lines[i]):
                            tb.rows.append(_split_table_row(lines[i]))
                            i += 1
                        blocks.append(tb)
                        continue
            except Exception:
                pass

        # 普通段落:收集到空行
        para_spans: List[Span] = []
        while i < n and lines[i].strip():
            if not para_spans:
                para_spans.extend(parse_inline(lines[i].strip()))
            else:
                para_spans.append(Span(text="\n"))
                para_spans.extend(parse_inline(lines[i].strip()))
            i += 1
        blocks.append(Block("paragraph", _merge(para_spans)))

    return blocks


def _split_table_row(line: str) -> List[str]:
    return [c.strip() for c in line.strip("|").split("|")]
