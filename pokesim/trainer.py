"""NPC 训练家队伍生成。

当用户/LLM 没有明确指定某 NPC 的宝可梦时,按「玩家当前队伍强度 +
训练家级别 + 属性主题」自动生成,使 NPC 强度贴合当前剧情进度。

用法要点:
  · 等级默认跟随玩家队首等级(tier/difficulty 提供修正);
  · 队伍规模由训练家级别决定(短裤小子 1-2 → 冠军 5-6);
  · 物种按 种族值是否贴近目标强度 + 进化阶段 加权抽取;
  · 默认排除传说/幻兽/究极异兽/悖谬种。
"""

from __future__ import annotations

import random

from .encounter import is_wild_candidate

# 训练家关键词(按优先级从上到下匹配)→ (队伍规模范围, 强度 0~1, 等级修正, 携道具率)
TIERS: list[tuple[tuple[str, ...], tuple[int, int], float, int, float]] = [
    (("冠军", "联盟冠军"), (5, 6), 1.0, 3, 0.7),
    (("四天王", "天王"), (4, 5), 0.85, 2, 0.6),
    (("道馆", "馆主"), (3, 4), 0.62, 1, 0.5),
    (("宿敌", "劲敌", "对手"), (2, 4), 0.5, 1, 0.35),
    (("精英", "干部", "队长", "保镖", "忍者", "秘书"), (2, 4), 0.5, 1, 0.35),
    (("训练家", "登山", "露营", "泳者", "研究员", "记者", "画家"), (2, 3), 0.25, 0, 0.15),
    (("短裤", "捕虫", "小学生", "路人", "观光", "钓客", "水手", "农夫"), (1, 2), 0.05, -1, 0.1),
]
DEFAULT_TIER = ((2, 3), 0.25, 0, 0.15)

# 携带道具池(高级训练家更常见)
HOLD_ITEMS = [
    "leftovers",
    "sitrus-berry",
    "lum-berry",
    "life-orb",
    "choice-band",
    "choice-specs",
    "choice-scarf",
    "focus-sash",
    "air-balloon",
    "assault-vest",
    "rocky-helmet",
    "expert-belt",
]

DIFFICULTY_DELTA = {"easy": -2, "简单": -2, "normal": 0, "普通": 0, "hard": 2, "困难": 2}


def tier_of(trainer: str) -> tuple[tuple[int, int], float, int, float]:
    name = str(trainer or "")
    for keys, size, power, delta, item_p in TIERS:
        if any(k in name for k in keys):
            return size, power, delta, item_p
    return DEFAULT_TIER


def theme_types(dex, theme: str = "", trainer: str = "") -> set[str]:
    """解析属性主题:显式 theme 优先,否则从训练家名里找属性名。"""
    zh2en = {v: k for k, v in (dex.type_zh or {}).items()}
    zh2en["一般"] = "Normal"

    def _scan(text: str) -> set[str]:
        found: set[str] = set()
        for zh, en in zh2en.items():
            if zh and zh in text:
                found.add(en)
        return found

    if theme:
        parts = str(theme).replace("、", "/").replace(",", "/").replace(" ", "/")
        got: set[str] = set()
        for part in parts.split("/"):
            part = part.strip()
            if not part:
                continue
            got |= _scan(part)
        if got:
            return got
    return _scan(str(trainer or ""))


def _stage(entry: dict) -> int:
    if not entry.get("prevo"):
        return 0
    return 1 if entry.get("evos") else 2


def _stage_mult(power: float, stage: int) -> float:
    low = {0: 2.0, 1: 1.0, 2: 0.3}
    high = {0: 0.5, 1: 1.2, 2: 2.2}
    return low[stage] * (1 - power) + high[stage] * power


def _bst(entry: dict) -> int:
    return sum(int(v) for v in (entry.get("baseStats") or {}).values())


def _eligible_at_level(entry: dict, level: int) -> bool:
    evo = entry.get("evoLevel")
    try:
        need = int(evo)
    except (TypeError, ValueError):
        return True
    return level >= need


def _sample_distinct(keys: list[str], weights: list[float], k: int, rng: random.Random) -> list[str]:
    pool = list(zip(keys, weights, strict=True))
    chosen: list[str] = []
    for _ in range(min(k, len(pool))):
        total = sum(w for _, w in pool)
        if total <= 0:
            chosen.extend(kk for kk, _ in pool)
            break
        r = rng.random() * total
        acc = 0.0
        pick = len(pool) - 1
        for i, (_kk, w) in enumerate(pool):
            acc += w
            if r <= acc:
                pick = i
                break
        chosen.append(pool[pick][0])
        pool.pop(pick)
    return chosen


def _pick_hold_item(entry: dict, power: float, rng: random.Random) -> str:
    if _stage(entry) < 2 and power >= 0.5 and rng.random() < 0.35:
        return "eviolite"
    return rng.choice(HOLD_ITEMS)


def generate_team(
    dex,
    *,
    party: list[dict],
    trainer: str = "",
    theme: str = "",
    level: int = 0,
    size: int = 0,
    difficulty: str = "",
    allow_rare: bool = False,
    rng: random.Random | None = None,
) -> dict:
    """生成一支 NPC 队伍。

    返回 {"team":[{species,zh,level,item,is_ace}] , "level":int, "size":int, "power":float, "theme":[en...]}
    """
    r = rng or random.Random()
    size_range, power, delta, item_p = tier_of(trainer)

    lead = 5
    for p in party:
        try:
            lead = int(p.get("level", 5) or 5)
        except (TypeError, ValueError):
            lead = 5
        break
    diff = DIFFICULTY_DELTA.get(str(difficulty).lower(), 0)
    lvl = int(level) if level and level > 0 else max(2, min(100, lead + delta + diff))

    n = int(size) if size and size > 0 else r.randint(*size_range)
    n = max(1, min(6, n))

    types = theme_types(dex, theme, trainer)
    target = 300 + 3.2 * lvl + power * 40
    target = max(280.0, min(620.0, target))

    keys: list[str] = []
    weights: list[float] = []
    for key, entry in dex.species.items():
        if not is_wild_candidate(entry):
            continue
        et = set(entry.get("types") or [])
        if types and not (et & types):
            continue
        if not _eligible_at_level(entry, lvl):
            continue
        rare = bool(entry.get("isLegendary") or entry.get("isMythical"))
        tags = set(entry.get("tags") or [])
        if (rare or tags & {"Ultra Beast", "Paradox"}) and not allow_rare:
            continue
        w = 1.0 / (1.0 + abs(_bst(entry) - target) / 45.0)
        w *= _stage_mult(power, _stage(entry))
        if rare:
            w *= 0.02
        if w > 0:
            keys.append(key)
            weights.append(w)
    if not keys:
        return {"team": [], "level": lvl, "size": 0, "power": power, "theme": sorted(types)}

    picked = _sample_distinct(keys, weights, n, r)
    picked.sort(key=lambda k: _bst(dex.species[k]))
    team: list[dict] = []
    for i, k in enumerate(picked):
        entry = dex.species[k]
        is_ace = i == len(picked) - 1
        mon_lvl = lvl + (2 if is_ace and power >= 0.85 else 1 if is_ace else 0)
        item = ""
        if r.random() < item_p + (0.15 if is_ace else 0):
            item = _pick_hold_item(entry, power, r)
        team.append(
            {
                "species": k,
                "zh": entry.get("zh") or entry.get("name") or k,
                "level": max(2, min(100, mon_lvl)),
                "item": item,
                "is_ace": is_ace,
            }
        )
    return {
        "team": team,
        "level": lvl,
        "size": len(team),
        "power": power,
        "theme": sorted(types),
    }


def team_to_enemy_string(team: list[dict], items_label=None) -> str:
    """转成 poke_battle_start 的 enemy 语法:名称|等级|招式|道具。"""
    chunks: list[str] = []
    for m in team:
        item = m.get("item") or ""
        label = items_label(item) if (item and items_label) else item
        chunks.append(f"{m['zh']}|{m['level']}||{label}" if label else f"{m['zh']}|{m['level']}")
    return ";".join(chunks)


__all__ = [
    "DIFFICULTY_DELTA",
    "HOLD_ITEMS",
    "generate_team",
    "team_to_enemy_string",
    "theme_types",
    "tier_of",
]
