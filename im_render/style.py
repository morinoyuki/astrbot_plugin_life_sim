"""主题、字体管理。

- 从环境变量 ``LIFE_SIM_FONT`` / 常见系统路径 / pillowmd 中探测中文字体
- 构建粗体 / 常规两个字重(找不到粗体时回退常规)
- 内置 MoToTalk 风格 light / dark 主题
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from PIL import ImageFont

__all__ = [
    "MOMOTOKI_DARK",
    "MOMOTOKI_LIGHT",
    "THEMES",
    "Theme",
    "clear_font_cache",
    "load_font",
    "load_title_font",
    "resolve_font_path",
]

# 常见 CJK 字体(环境变量 LIFE_SIM_FONT 优先级最高)
_OS_FONT_CANDIDATES: tuple[str, ...] = (
    # Linux
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/wenquanyi/wqy-zenhei.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    # Windows
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
)


def _discover_font_candidates() -> tuple[str, ...]:
    """构建字体候选:环境变量 > styles 模板字体 > 系统字体 > pillowmd 内置雅黑。

    优先级:
    1. 环境变量 LIFE_SIM_FONT
    2. 插件根目录 fonts/ 下的共用字体(用户放置)
    3. 同环境已安装的 pillowmd 内置雅黑
    4. 系统常见 CJK 字体
    """
    found: list[str] = []

    def add(p: str) -> None:
        if p and os.path.isfile(p) and p not in found:
            found.append(p)

    # 1. styles 模板字体(OPPOSans / 仓耳小丸子等,按优先级)
    #    尝试定位插件根目录(sty les/ 文件)
    _here = os.path.dirname(os.path.abspath(__file__))  # .../im_render
    # 插件根目录 fonts/ 优先(用户移出的共用字体目录)
    for base in (os.path.dirname(_here), os.getcwd()):
        _fonts = os.path.join(base, "fonts")
        if os.path.isdir(_fonts):
            try:
                for fn in sorted(os.listdir(_fonts)):
                    if fn.lower().endswith((".ttf", ".ttc", ".otf")):
                        add(os.path.join(_fonts, fn))
            except OSError:
                pass

    # 2. pillowmd 内置雅黑(优先于系统字体,通常字体更全)
    try:
        import pillowmd  # type: ignore

        pm = os.path.join(
            os.path.dirname(os.path.abspath(pillowmd.__file__)),
            "data",
            "fonts",
            "yahei.ttf",
        )
        add(pm)
    except Exception:
        pass

    # 3. 系统字体
    for p in _OS_FONT_CANDIDATES:
        add(p)

    return tuple(found)


FALLBACK_FONT_CANDIDATES: tuple[str, ...] = _discover_font_candidates()

BOLD_FONT_CANDIDATES: tuple[str, ...] = (
    "C:/Windows/Fonts/msyhbd.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf",
    "C:/Windows/Fonts/simhei.ttf",
    *tuple(p for p in FALLBACK_FONT_CANDIDATES),
)

# 标题专用字体(仓耳小丸子等圆体),按文件名关键词逐个探测
_TITLE_FONT_NAMES = ("仓耳", "丸子", "圆体", "comic", "pixel")

_font_path: str | None = None
_bold_font_path: str | None = None
_title_font_path: str | None = None
_font_searched = False


def _find_font(candidates) -> str | None:
    for p in candidates:
        try:
            if p and os.path.isfile(p) and os.access(p, os.R_OK):
                ImageFont.truetype(p, 12)
                return p
        except (OSError, ValueError):
            continue  # 字体文件损坏或不可用,试下一个候选
    return None


def _find_title_font() -> str | None:
    """从字体候选里挑选标题专用字体(仓耳小丸子等)。"""
    for p in FALLBACK_FONT_CANDIDATES:
        name = os.path.basename(p).lower()
        if any(k in name for k in _TITLE_FONT_NAMES):
            try:
                if os.path.isfile(p) and os.access(p, os.R_OK):
                    ImageFont.truetype(p, 12)
                    return p
            except (OSError, ValueError):
                continue
    return None


def search_fonts() -> None:
    global _font_searched, _font_path, _bold_font_path, _title_font_path
    if _font_searched:
        return
    env = os.environ.get("LIFE_SIM_FONT", "").strip()

    reg: list = [env] if env else []
    reg += list(FALLBACK_FONT_CANDIDATES)
    _font_path = _find_font(reg)
    _bold_font_path = _find_font(([env] if env else []) + list(BOLD_FONT_CANDIDATES))
    _title_font_path = _find_title_font() or _font_path
    _font_searched = True


def resolve_font_path() -> str | None:
    search_fonts()
    return _font_path


@lru_cache(maxsize=64)
def _cached_truetype(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, int(size))


@lru_cache(maxsize=64)
def _cached_default(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=int(size))
    except TypeError:
        return ImageFont.load_default()


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    search_fonts()
    size = max(4, int(size))
    path = (_bold_font_path if (bold and _bold_font_path) else None) or _font_path
    if path:
        try:
            return _cached_truetype(path, size)
        except OSError:
            pass  # 主字体失效时回退到默认字体
    return _cached_default(size)


def load_title_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """加载标题专用字体(仓耳小丸子等圆体);没找到时回退主字体。"""
    search_fonts()
    size = max(4, int(size))
    path = _title_font_path or _font_path
    if path:
        try:
            return _cached_truetype(path, size)
        except OSError:
            pass
    return load_font(size, bold)


def clear_font_cache() -> None:
    global _emoji_fonts_cache
    _cached_truetype.cache_clear()
    _cached_default.cache_clear()
    _emoji_fonts_cache = None


# ── emoji / 符号字体回退 ────────────────────────────────────────────
# 主中文字体(OPPO Sans 等)缺少 emoji 字形时,回退到 Symbola_hint.ttf(表情/符号字体)。
# 用户可在字体目录放置 Symbola_hint.ttf(默认随插件 fonts/ 提供)。
# 若 Symbola 也不含该字符(如较新 emoji),则按 ``_EMOJI_FALLBACK_NAMES`` 扩展现有字体。

# 额外的 emoji 覆盖字体文件名(优先级依次)。放置任意一个到 fonts/ 目录即可扩展回退:
#   - NotoColorEmoji-Regular.ttf  (Google,覆盖几乎全部现代 emoji)
#   - Segoe UI Emoji / AppleColorEmoji / OpenMoji / Twemoji 同理
_EMOJI_FALLBACK_NAMES = (
    "NotoColorEmoji-Regular.ttf",
    "NotoColorEmoji.ttf",
    "Segoe UI Emoji.ttf",
    "SegoeUIEmoji.ttf",
    "OpenMoji-black.ttf",
    "OpenMoji-black.otf",
    "TwemojiSans.ttf",
    "NotoEmoji-Regular.ttf",
    "Apple Color Emoji.ttc",
)

# 随系统自带的 emoji / 符号字体路径(优先级最高)。
# 自带 Symbola 只覆盖 2014 年以前的 emoji,较新的 🥺/🤔/🙄 等缺失;
# Windows 的 Segoe UI Emoji / macOS 的 Apple Color Emoji / Linux 的 NotoColorEmoji
# 等能覆盖几乎所有现代 emoji,有的话优先选用。
_OS_EMOJI_FONT_CAND = (
    # Windows
    "C:/Windows/Fonts/Segoe UI Emoji.ttf",
    "C:/Windows/Fonts/SegoeUIEmoji.ttf",
    "C:/Windows/Fonts/NotoColorEmoji-Regular.ttf",
    "C:/Windows/Fonts/NotoColorEmoji.ttf",
    # macOS
    "/System/Library/Fonts/Apple Color Emoji.ttc",
    "/Library/Fonts/Apple Color Emoji.ttc",
    # Linux
    "/usr/share/fonts/opentype/noto/NotoColorEmoji-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji-Regular.ttf",
    "/usr/share/fonts/noto/NotoColorEmoji-Regular.ttf",
    "/usr/share/fonts/noto-cjk/NotoColorEmoji-Regular.ttf",
    "/usr/share/fonts/twemoji/TwitterColorEmoji-SVGinOT.ttf",
    "/usr/share/fonts/truetype/noto/NotoEmoji-Regular.ttf",
)

# 额外的系统字体目录,按文件名扫描其中的 emoji 字体(兜底)。
_OS_EMOJI_FONT_DIRS = (
    "C:/Windows/Fonts",
    "/System/Library/Fonts",
    "/Library/Fonts",
    "/usr/share/fonts",
    "/usr/share/fonts/truetype/noto",
    "/usr/share/fonts/opentype/noto",
)


def _is_emoji_control(ch: str) -> bool:
    """变体选择符 / 零宽连接符等组合控制码点。

    这类码点本身没有可见字形,但与前一字符组合成一个视觉 emoji(如 ``❤️``=
    ❤+FE0F、阶级 ZWJ 序列)。当前逐字符渲染无法合成彩色 emoji,若单独绘制
    会被字体当作缺字符打出方块,故绘制时应直接跳过。
    """
    if not ch:
        return True
    cp = ord(ch)
    return (
        cp in (0x200B, 0x200C, 0x200D, 0x200E, 0xFE00, 0xFEFF)  # ZWJ/ZWNJ/单位/字节序
        or 0xFE00 <= cp <= 0xFE0F  # 变体选择符
        or 0xE0100 <= cp <= 0xE01EF  # 补充变体选择符
    )


_cmap_cache: dict[str, set | None] = {}


def _charset(path: str) -> set | None:
    """读取字体的 cmap 码点集合(缓存)。"""
    if path not in _cmap_cache:
        try:
            from fontTools.ttLib import TTFont

            f = TTFont(path, fontNumber=0)
            cmap = f.getBestCmap()
            _cmap_cache[path] = set(cmap.keys()) if cmap else set()
        except Exception:
            _cmap_cache[path] = None
    return _cmap_cache[path]


def _supports(path: str | None, char: str) -> bool:
    if not path:
        return True
    s = _charset(path)
    return bool(s and ord(char) in s)


_emoji_fonts_cache: list[str] | None = None


def _discover_emoji_fonts() -> list[str]:
    """发现备用符号/emoji 字体(secondFonts 模型),按优先级排列。

    优先级:
    1. 系统自带的彩色 emoji 字体(Segoe UI Emoji / Apple Color Emoji /
       NotoColorEmoji 等,覆盖几乎全部现代 emoji)。若系统有,优先选用;
    2. 按文件名扫描系统字体目录(兜底命中其它命名/路径);
    3. 随插件 fonts/ 提供的黑白 Symbola(符号覆盖较全,但新版 emoji 缺失)。
    返回可用的字体路径列表(先探测存在的)。
    """
    global _emoji_fonts_cache
    if _emoji_fonts_cache is not None:
        return list(_emoji_fonts_cache)
    here = os.path.dirname(os.path.abspath(__file__))
    bases = [os.path.dirname(here), here, os.getcwd()]
    found: list[str] = []

    def add(p: str) -> None:
        if p and os.path.isfile(p) and p not in found:
            found.append(p)

    # 1) 系统自带 emoji 字体优先
    for p in _OS_EMOJI_FONT_CAND:
        add(p)
    # 2) 按文件名扫描常见系统字体目录(有些字体在子目录 / 命名略异)
    for d in _OS_EMOJI_FONT_DIRS:
        for fn in _EMOJI_FALLBACK_NAMES:
            add(os.path.join(d, fn))
    # 3) 随插件 fonts/ 提供的 Symbola 等黑白符号字体
    for base in bases:
        for sub in ("fonts", "font", ""):
            d = os.path.join(base, sub)
            for fn in _EMOJI_FALLBACK_NAMES + ("Symbola_hint.ttf", "Symbola.ttf"):
                add(os.path.join(d, fn))
    _emoji_fonts_cache = found
    return list(found)


def emoji_font_for(char: str, size: int) -> ImageFont.FreeTypeFont | None:
    """主字体缺失该字符时,从备用字体列表中选第一个覆盖它的(secondFonts 模型)。"""
    search_fonts()
    if _supports(_font_path, char):
        return None  # 主字体有字形
    for alt in _discover_emoji_fonts():
        try:
            if _supports(alt, char):
                return _cached_truetype(alt, size)
        except Exception:
            continue  # 该字体无法读取 / 加载失败,试下一个
    return None  # 所有备用字体都没有 → 由调用方回主字体(尽力)


def font_for_char(char: str, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """按字符选择字体:主字体有字形用主, 否则在备用符号/emoji 字体中选第一个能覆盖的。"""
    ef = emoji_font_for(char, size)
    if ef is not None:
        return ef
    return load_font(size, bold)


# ═════════════════════════════════════════════════════════════════
# 主题
# ═════════════════════════════════════════════════════════════════


@dataclass
class Theme:
    """主题调色板。"""

    name: str
    bg_top: str
    bg_bottom: str
    text: str
    text_secondary: str
    text_muted: str
    bubble_self: str
    bubble_self_text: str
    bubble_other: str
    bubble_other_text: str
    name_color: str
    pill_bg: str
    pill_text: str
    code_bg: str
    card_bg: str
    card_border: str
    link: str
    header_bg: str
    header_text: str


MOMOTOKI_LIGHT = Theme(
    name="momotoki-light",
    bg_top="#DDE7F2",
    bg_bottom="#EEF3F8",
    text="#1F2A33",
    text_secondary="#5A6B7A",
    text_muted="#97A5B0",
    bubble_self="#4A8AC6",
    bubble_self_text="#FFFFFF",
    bubble_other="#FFFFFF",
    bubble_other_text="#2B3A45",
    name_color="#6E7F8E",
    pill_bg="#E7EDF3",
    pill_text="#5C6B78",
    code_bg="#EDF1F5",
    card_bg="#FFFFFF",
    card_border="#DCE4EC",
    link="#4A8AC6",
    header_bg="#3D6A93",
    header_text="#FFFFFF",
)

MOMOTOKI_DARK = Theme(
    name="momotoki-dark",
    bg_top="#232A33",
    bg_bottom="#1A2026",
    text="#EAF0F5",
    text_secondary="#A9B6C1",
    text_muted="#6E7A85",
    bubble_self="#3B6FA8",
    bubble_self_text="#F5F9FC",
    bubble_other="#2C353D",
    bubble_other_text="#D6DEE6",
    name_color="#8FA0AE",
    pill_bg="#2B343C",
    pill_text="#9DAAB6",
    code_bg="#232C34",
    card_bg="#2A343D",
    card_border="#3A4650",
    link="#6FA8DA",
    header_bg="#2A3846",
    header_text="#EDF2F6",
)

THEMES = {
    "light": MOMOTOKI_LIGHT,
    "dark": MOMOTOKI_DARK,
}


def _hex(color: str) -> tuple:
    """#RRGGBB -> (r,g,b)"""
    c = (color or "#888888").lstrip("#")
    if len(c) == 3:
        c = "".join(x * 2 for x in c)
    try:
        return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))
    except ValueError:
        return (136, 136, 136)


def rgba(color: str, alpha: int = 255) -> tuple:
    """#RRGGBB -> (r,g,b,a)"""
    r, g, b = _hex(color)
    return (r, g, b, alpha)
