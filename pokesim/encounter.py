"""野生宝可梦遭遇生成。

LLM 只需要给出「地点/生态」关键词,由本地图鉴数据按权重抽取物种,
避免把上千种宝可梦塞进上下文,也避免乱放传说/地点不搭的宝可梦。

权重因素:
  · 出现率(captureRate 越高越常见)
  · 进化阶段(初始形态常见,最终形态稀少)
  · 种族值总和(越强越稀少)
  · 生态属性匹配(草地=草/虫/一般…)
  · 昼夜/特殊修正
传说/幻兽默认排除,除非 allow_rare。
"""

from __future__ import annotations

import random

# 地区 → (全国图鉴号范围, 该地区的地区形态 forme 名)
REGIONS: dict[str, tuple[int, int, str]] = {
    "关都": (1, 151, ""),
    "关东": (1, 151, ""),
    "城都": (152, 251, ""),
    "成都": (152, 251, ""),
    "丰缘": (252, 386, ""),
    "丰原": (252, 386, ""),
    "神奥": (387, 493, ""),
    "合众": (494, 649, ""),
    "伊修": (494, 649, ""),
    "卡洛斯": (650, 721, ""),
    "阿罗拉": (722, 809, "Alola"),
    "伽勒尔": (810, 905, "Galar"),
    "伽勒尔地区": (810, 905, "Galar"),
    "帕底亚": (906, 1025, "Paldea"),
    "帕尔迪亚": (906, 1025, "Paldea"),
}

# 世代 → 全国图鉴号范围
GENS: dict[int, tuple[int, int]] = {
    1: (1, 151),
    2: (152, 251),
    3: (252, 386),
    4: (387, 493),
    5: (494, 649),
    6: (650, 721),
    7: (722, 809),
    8: (810, 905),
    9: (906, 1025),
}

# 生态 → 常见属性(命中任一即入选)
BIOME_TYPES: dict[str, set[str]] = {
    "草地": {"Grass", "Bug", "Normal", "Flying", "Fairy", "Ground"},
    "草原": {"Grass", "Normal", "Ground", "Flying"},
    "森林": {"Grass", "Bug", "Flying", "Dark", "Fairy"},
    "树林": {"Grass", "Bug", "Flying", "Dark"},
    "丛林": {"Grass", "Bug", "Poison", "Fighting"},
    "洞窟": {"Rock", "Ground", "Steel", "Dark", "Poison", "Dragon"},
    "洞穴": {"Rock", "Ground", "Steel", "Dark", "Poison"},
    "山洞": {"Rock", "Ground", "Steel", "Dark"},
    "矿洞": {"Rock", "Ground", "Steel"},
    "水面": {"Water"},
    "水面/海": {"Water"},
    "海": {"Water"},
    "河": {"Water"},
    "湖": {"Water"},
    "池塘": {"Water"},
    "海滩": {"Water", "Ground", "Flying"},
    "沙漠": {"Ground", "Rock", "Fire", "Dragon"},
    "雪原": {"Ice"},
    "雪山": {"Ice", "Rock"},
    "冰洞": {"Ice", "Rock"},
    "火山": {"Fire", "Rock", "Ground"},
    "城市": {"Normal", "Electric", "Poison", "Fairy", "Dark", "Fighting"},
    "城镇": {"Normal", "Electric", "Poison", "Fairy"},
    "道路": {"Normal", "Flying", "Bug", "Grass", "Ground", "Fighting"},
    "遗迹": {"Psychic", "Ghost", "Ground", "Rock", "Fairy"},
    "废墟": {"Psychic", "Ghost", "Dark", "Rock"},
    "墓园": {"Ghost", "Dark", "Poison"},
    "天空": {"Flying", "Dragon"},
    "沼泽": {"Water", "Poison", "Ground", "Bug"},
    "电场": {"Electric", "Steel"},
    "发电厂": {"Electric", "Steel", "Poison"},
    "道馆": {"Fighting", "Psychic", "Steel", "Fire", "Water", "Electric"},
    "工厂": {"Steel", "Electric", "Poison", "Normal"},
    "农场": {"Normal", "Grass", "Ground", "Flying"},
    "花园": {"Grass", "Fairy", "Bug"},
    "塔": {"Psychic", "Ghost", "Flying", "Dragon"},
}

# 关键词 → 修正(夜间/特殊)
NIGHT_BOOST = {"Dark", "Ghost", "Poison", "Psychic"}
NIGHT_KEYS = ("夜晚", "夜间", "夜里", "午夜", "深夜", "晚上", "月")

# 排除的形态(超级进化/极巨化/原始回归/究极爆发等,非野生)
_FORM_EXCLUDE = ("Mega", "Gmax", "Primal", "Eternamax", "Totem", "Ash")

STAT_LABELS = {
    "hp": "HP",
    "atk": "攻击",
    "def": "防御",
    "spa": "特攻",
    "spd": "特防",
    "spe": "速度",
}


def is_wild_candidate(entry: dict) -> bool:
    if entry.get("battleOnly"):
        return False
    if int(entry.get("num", 0) or 0) <= 0:
        return False
    forme = str(entry.get("forme") or "")
    return not any(x in forme for x in _FORM_EXCLUDE)


def regional_tag(entry: dict) -> str:
    """该条目若为地区形态,返回形态地区名(如 Alola),否则空串。"""
    forme = str(entry.get("forme") or "")
    for tag in ("Alola", "Galar", "Hisui", "Paldea"):
        if tag in forme:
            return tag
    return ""


def in_scope(entry: dict, region: str = "", gen: int = 0) -> bool:
    """条目是否落在指定地区/世代的全国图鉴范围内(含地区形态归属)。"""
    num = int(entry.get("num", 0) or 0)
    gen_range = GENS.get(gen) if gen else None
    if gen_range and not (gen_range[0] <= num <= gen_range[1]):
        return False
    reg = REGIONS.get(region) if region else None
    if reg:
        if not (reg[0] <= num <= reg[1]):
            return False
        etag = regional_tag(entry)
        if etag and etag != reg[2]:
            return False
    return True


def _stage_weight(entry: dict) -> float:
    has_prevo = bool(entry.get("prevo"))
    has_evo = bool(entry.get("evos"))
    if not has_prevo and has_evo:
        return 1.0  # 初始形态:常见
    if has_prevo and has_evo:
        return 0.55  # 中间形态
    if has_prevo and not has_evo:
        return 0.22  # 最终形态:稀少
    return 0.7  # 无进化线的单体


def _power_weight(entry: dict) -> float:
    total = sum(int(v) for v in (entry.get("baseStats") or {}).values())
    if total >= 580:
        return 0.05
    if total >= 500:
        return 0.18
    if total >= 450:
        return 0.45
    if total >= 400:
        return 0.75
    return 1.0


def biome_types(area: str) -> set[str]:
    """从自由文本地点里解析出生态属性集合。"""
    types: set[str] = set()
    for key, ts in BIOME_TYPES.items():
        if key in area:
            types |= ts
    return types


def is_night(area: str) -> bool:
    return any(k in area for k in NIGHT_KEYS)


def _pool(
    dex,
    *,
    area: str,
    region: str,
    gen: int,
    allow_rare: bool,
) -> list[tuple[str, float]]:
    dtypes = biome_types(area)
    night = is_night(area)
    reg = REGIONS.get(region) if region else None
    reg_tag = reg[2] if reg else ""

    pool: list[tuple[str, float]] = []
    for key, entry in dex.species.items():
        if not is_wild_candidate(entry):
            continue
        if not in_scope(entry, region, gen):
            continue
        if (entry.get("isLegendary") or entry.get("isMythical")) and not allow_rare:
            continue
        tags = set(entry.get("tags") or [])
        if tags & {"Ultra Beast", "Paradox"} and not allow_rare:
            continue
        types = set(entry.get("types") or [])
        if dtypes and not (types & dtypes):
            continue

        weight = float(entry.get("captureRate", 45) or 45)
        weight *= _stage_weight(entry)
        weight *= _power_weight(entry)
        if night and (types & NIGHT_BOOST):
            weight *= 2.0
        if reg and reg_tag and regional_tag(entry) == reg_tag:
            weight *= 1.6
        if allow_rare and (entry.get("isLegendary") or entry.get("isMythical")):
            weight *= 0.01
        if weight > 0:
            pool.append((key, weight))
    return pool


def _rarity(entry: dict) -> str:
    rate = int(entry.get("captureRate", 45) or 45)
    if entry.get("isLegendary"):
        return "传说"
    if entry.get("isMythical"):
        return "幻之"
    if rate <= 25:
        return "极稀有"
    if rate <= 75:
        return "稀有"
    if rate <= 150:
        return "较少见"
    return "常见"


_WATER_KEYS = ("水面", "海", "河", "湖", "池", "潜", "游泳", "水边", "汀")
_FISH_KEYS = ("钓", "钓鱼", "渔")


def detect_environment(area: str) -> str:
    """从地点文本判断遭遇环境:land / water / fish / all。"""
    a = str(area or "")
    if any(k in a for k in _FISH_KEYS):
        return "fish"
    if any(k in a for k in _WATER_KEYS):
        return "water"
    return "land"


def roll_location_encounter(
    dex,
    loc_key: str,
    *,
    version_group: str = "",
    environment: str = "",
    level: int = 0,
    include_special: bool = False,
    rng: random.Random | None = None,
) -> dict | None:
    """从真实地点分布里抽取一只野生宝可梦。"""
    from .dex import FISH_METHODS, LAND_METHODS, WATER_GROUP

    r = rng or random.Random()
    env = environment or "land"
    methods = {
        "water": WATER_GROUP,
        "fish": FISH_METHODS,
        "land": LAND_METHODS,
    }.get(env)
    pools = dex.location_pools(
        loc_key, version_group, methods=methods, include_special=include_special
    )
    if not pools:
        return None
    merged: dict[str, dict] = {}
    for p in pools:
        e = merged.setdefault(
            p["species"],
            {
                "species": p["species"],
                "min": p["min"],
                "max": p["max"],
                "chance": 0,
                "methods": set(),
            },
        )
        e["min"] = min(e["min"], p["min"])
        e["max"] = max(e["max"], p["max"])
        e["chance"] = max(e["chance"], p["chance"])
        e["methods"].add(p["method"])
    items = list(merged.values())
    weights = [max(1, int(i["chance"])) for i in items]
    pick = r.choices(items, weights=weights, k=1)[0]
    entry = dex.species.get(pick["species"]) or {}
    lo, hi = int(pick["min"]), int(pick["max"])
    lvl = int(level) if level and level > 0 else r.randint(lo, max(lo, hi))
    return {
        "species": pick["species"],
        "name": entry.get("name", pick["species"]),
        "zh": entry.get("zh") or entry.get("name", pick["species"]),
        "types": list(entry.get("types") or []),
        "level": lvl,
        "min": lo,
        "max": hi,
        "methods": sorted(pick["methods"]),
        "rarity": _rarity(entry),
        "location": loc_key,
    }


def roll_encounter(
    dex,
    *,
    area: str = "",
    region: str = "",
    gen: int = 0,
    level: int = 0,
    allow_rare: bool = False,
    rng: random.Random | None = None,
) -> dict | None:
    """按生态抽取一只野生宝可梦。

    返回 {"species","level","name","zh","types","rarity"} 或 None(无匹配)。
    level<=0 时不在此处决定(由调用方按队伍等级给出)。
    """
    r = rng or random.Random()
    pool = _pool(dex, area=area, region=region, gen=gen, allow_rare=allow_rare)
    if not pool:
        return None
    keys = [k for k, _ in pool]
    weights = [w for _, w in pool]
    key = r.choices(keys, weights=weights, k=1)[0]
    entry = dex.species[key]
    return {
        "species": key,
        "name": entry.get("name", key),
        "zh": entry.get("zh") or entry.get("name", key),
        "types": list(entry.get("types") or []),
        "rarity": _rarity(entry),
        "level": int(level or 0),
    }


def roll_level(dex, party: list[dict], *, level: int = 0, rng: random.Random | None = None) -> int:
    """决定野生宝可梦等级:显式 > 按队首等级浮动。"""
    if level and level > 0:
        return max(2, min(100, int(level)))
    r = rng or random.Random()
    lead = 5
    for p in party:
        try:
            lead = int(p.get("level", 5) or 5)
        except (TypeError, ValueError):
            lead = 5
        break
    return max(2, min(100, lead + r.randint(-3, 2)))


__all__ = [
    "BIOME_TYPES",
    "GENS",
    "REGIONS",
    "biome_types",
    "detect_environment",
    "in_scope",
    "is_night",
    "is_wild_candidate",
    "regional_tag",
    "roll_encounter",
    "roll_level",
    "roll_location_encounter",
]
