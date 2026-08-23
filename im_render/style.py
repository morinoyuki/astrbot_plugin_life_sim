"""主题、字体管理。

- 从环境变量 ``LIFE_SIM_FONT`` / 常见系统路径 / pillowmd 中探测中文字体
- 构建粗体 / 常规两个字重(找不到粗体时回退常规)
- 内置 MoToTalk 风格 light / dark 主题
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional, Tuple

from PIL import ImageFont

__all__ = [
    "resolve_font_path",
    "load_font",
    "clear_font_cache",
    "MOMOTOKI_LIGHT",
    "MOMOTOKI_DARK",
    "THEMES",
    "Theme",
]

# 常见 CJK 字体(环境变量 LIFE_SIM_FONT 优先级最高)
_OS_FONT_CANDIDATES: Tuple[str, ...] = (
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


def _discover_font_candidates() -> Tuple[str, ...]:
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


FALLBACK_FONT_CANDIDATES: Tuple[str, ...] = _discover_font_candidates()

BOLD_FONT_CANDIDATES: Tuple[str, ...] = (
    "C:/Windows/Fonts/msyhbd.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf",
    "C:/Windows/Fonts/simhei.ttf",
    *tuple(p for p in FALLBACK_FONT_CANDIDATES),
)

_font_path: Optional[str] = None
_bold_font_path: Optional[str] = None
_font_searched = False


def _find_font(candidates) -> Optional[str]:
    for p in candidates:
        try:
            if p and os.path.isfile(p) and os.access(p, os.R_OK):
                ImageFont.truetype(p, 12)
                return p
        except Exception:
            continue
    return None


def search_fonts() -> None:
    global _font_searched, _font_path, _bold_font_path
    if _font_searched:
        return
    env = os.environ.get("LIFE_SIM_FONT", "").strip()

    reg: list = [env] if env else []
    reg += list(FALLBACK_FONT_CANDIDATES)
    _font_path = _find_font(reg)
    _bold_font_path = _find_font(([env] if env else []) + list(BOLD_FONT_CANDIDATES))
    _font_searched = True


def resolve_font_path() -> Optional[str]:
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
        except Exception:
            pass
    return _cached_default(size)


def clear_font_cache() -> None:
    _cached_truetype.cache_clear()
    _cached_default.cache_clear()


# ── emoji / 符号字体回退 ────────────────────────────────────────────
# 主中文字体(OPPO Sans 等)缺少 emoji 字形时,回退到 Symbola_hint.ttf(表情/符号字体)。
# 用户可在字体目录放置 Symbola_hint.ttf(默认随插件 fonts/ 提供)。

_emoji_font_path: Optional[str] = None


def _discover_emoji_font() -> Optional[str]:
    """在字体搜索目录(含 fonts/)中查找 Symbola / Symbola_hint 等 emoji 字体。"""
    global _emoji_font_path
    if _emoji_font_path is not None:
        return _emoji_font_path or None
    here = os.path.dirname(os.path.abspath(__file__))
    bases = [os.path.dirname(here), here, os.getcwd()]
    for base in bases:
        for sub in ("fonts", "font", ""):
            d = os.path.join(base, sub)
            for fn in ("Symbola_hint.ttf", "Symbola.ttf", "NotoEmoji-Regular.ttf"):
                fp = os.path.join(d, fn)
                if os.path.isfile(fp):
                    _emoji_font_path = fp
                    return fp
    _emoji_font_path = ""
    return None


_cmap_cache: dict[str, Optional[set]] = {}


def _charset(path: str) -> Optional[set]:
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


def _supports(path: Optional[str], char: str) -> bool:
    if not path:
        return True
    s = _charset(path)
    return bool(s and ord(char) in s)


def emoji_font_for(char: str, size: int) -> Optional[ImageFont.FreeTypeFont]:
    """若主字体缺失该字符且 emoji 字体有,返回 emoji 字体;否则 None。"""
    search_fonts()
    em = _discover_emoji_font()
    if not em:
        return None
    if not _supports(em, char):
        return None
    if _supports(_font_path, char):
        return None
    return _cached_truetype(em, size)


def font_for_char(char: str, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """按字符选择字体:主字体无字形时回退到 Symbola emoji 字体。"""
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
    except Exception:
        return (136, 136, 136)


def rgba(color: str, alpha: int = 255) -> tuple:
    """#RRGGBB -> (r,g,b,a)"""
    r, g, b = _hex(color)
    return (r, g, b, alpha)
