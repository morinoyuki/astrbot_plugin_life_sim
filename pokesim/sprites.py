"""宝可梦缩略图(头像用)。

图片在 ``static/sprites/<species_key>.png``(96x96 官方像素图,构建脚本:
``tools/build_pokemon_sprites.py``)。运行时只读本地文件、不联网。

用途:聊天卡片渲染时,若说话人名能解析成宝可梦,就默认用该缩略图当头像
(优先于全局默认头像、低于用户通过 /头像 设置的角色专属头像)。
"""

from __future__ import annotations

import os
import re
from functools import lru_cache

from .dex import get_dex

SPRITES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "sprites")

# 说话人标签:<d name="角色名" ...>
_SPEAKER_RE = re.compile(r'name\s*=\s*"([^"]*)"', re.IGNORECASE)
_WS_RE = re.compile(r"[\s\u3000]")


@lru_cache(maxsize=1)
def _available() -> frozenset[str]:
    try:
        return frozenset(f[:-4] for f in os.listdir(SPRITES_DIR) if f.endswith(".png"))
    except OSError:
        return frozenset()


def available_count() -> int:
    return len(_available())


def sprite_path(species_key: str) -> str:
    """宝可梦 key → 本地缩略图路径(没有则空串)。"""
    if not species_key:
        return ""
    if species_key not in _available():
        return ""
    return os.path.join(SPRITES_DIR, f"{species_key}.png")


def clean_speaker_name(name: str) -> str:
    n = _WS_RE.sub("", str(name or ""))
    n = n.split("(")[0].split("（")[0].strip()
    return n


def sprite_for_name(name: str) -> str:
    """说话人名 → 宝可梦缩略图路径;不是宝可梦则空串。"""
    clean = clean_speaker_name(name)
    if not clean:
        return ""
    try:
        dex = get_dex()
        r = dex.resolve_species(clean)
    except Exception:
        return ""
    if not r:
        return ""
    key, entry = r
    path = sprite_path(key)
    if path:
        return path
    base = entry.get("baseSpecies")
    if base:
        b = dex.resolve_species(base)
        if b:
            return sprite_path(b[0])
    return ""


def speaker_names(text: str) -> list[str]:
    """从聊天卡片文本里取出所有 <d name="…"> 的说话人名(去重、保序)。"""
    out: list[str] = []
    seen: set[str] = set()
    for m in _SPEAKER_RE.finditer(text or ""):
        name = clean_speaker_name(m.group(1))
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def pokemon_avatars_for_text(text: str, existing: dict | None = None) -> dict:
    """给文本里出现的宝可梦说话人补上缩略图头像(不覆盖已有)。

    existing: 角色名 → 头像(路径/URL/PIL Image),用户设置的头像优先。
    返回新的映射(含原内容)。
    """
    avatars: dict = dict(existing or {})
    for name in speaker_names(text):
        if name in avatars:
            continue
        path = sprite_for_name(name)
        if path:
            avatars[name] = path
    return avatars


__all__ = [
    "SPRITES_DIR",
    "available_count",
    "clean_speaker_name",
    "pokemon_avatars_for_text",
    "speaker_names",
    "sprite_for_name",
    "sprite_path",
]
