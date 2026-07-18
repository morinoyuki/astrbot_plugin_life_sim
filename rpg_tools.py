"""RPG 工具(从 astrbot_plugin_rpg_calc 整合而来)

代码组织:
    1. 配置常量      ── 世界成长规则、槽位 / 属性名翻译
    2. 存储层        ── 角色存档与会话的 load / save / 迁移
    3. 数值层        ── 升级、经验公式、属性点播洒
    4. 装备层        ── 装备属性 / 条件解析、收益应用与回退
    5. 显示层        ── 状态文本格式化
    6. RPGMixin      ── 19 个 rpg_* LLM 工具 + 私有助手
"""

import json
import os
import random
import re
import time

from astrbot.api import logger
from astrbot.api.event import filter

# ════════════════════════════════════════════════════════════════════
# 1. 配置常量
# ════════════════════════════════════════════════════════════════════

# 装备槽位的本地化名(出现于格式化输出与 LLM 调用提示)
SLOT_CN = {
    "weapon": "武器",
    "armor": "护甲",
    "accessory": "饰品",
    "main_weapon": "主武器",
    "sub_weapon": "副武器",
    "tactical_glasses": "战术眼镜",
    "helmet": "头盔",
    "boots": "靴子",
    "gloves": "手套",
    "ring": "戒指",
    "necklace": "项链",
    "shield": "盾牌",
}

# 角色字典里会被 _format_status 当作「内置键」跳过(避免重复显示)
BUILTIN_CHAR_KEYS = frozenset(
    {
        "name",
        "world",
        "world_rules",
        "class",
        "level",
        "exp",
        "hp",
        "max_hp",
        "atk",
        "def",
        "spd",
        "skills",
        "equipment",
        "buffs",
        "debuffs",
        "inventory",
        "currency",
        "kills",
        "unspent_points",
        "attr_points_per_level",
        "session_id",
        "save_info",
        "game_system",
        "hit_die",
        "ability_score_rolls",
    }
)

DEFAULT_WORLD_RULES = {
    "base_hp": 100,
    "base_atk": 7,
    "base_def": 5,
    "base_spd": 5,
    "hp_lv": 13,
    "atk_lv": 2,
    "def_lv": 2,
    "spd_lv": 1,
    "exp_base": 100,
    "exp_scale": 1.2,
    "auto_stat_growth": 0,
    # 各世界可独立定义自己的额外属性列表(如 GGO 的 STR/VIT/...)。
    # 默认空,表示该世界没有额外属性 — 自动成长与加点都跳过。
    "stats": [],
    "classes": {},
    "skills": [],
    "currency_name": "金币",
}

DND5E_ABILITIES = ("STR", "DEX", "CON", "INT", "WIS", "CHA")
DND5E_ASI_LEVELS = frozenset({4, 8, 12, 16, 19})
DND5E_CLASS_HIT_DICE = {
    "barbarian": 12,
    "野蛮人": 12,
    "fighter": 10,
    "战士": 10,
    "paladin": 10,
    "圣武士": 10,
    "ranger": 10,
    "游侠": 10,
    "warlock": 8,
    "邪术师": 8,
    "cleric": 8,
    "牧师": 8,
    "druid": 8,
    "德鲁伊": 8,
    "bard": 8,
    "吟游诗人": 8,
    "rogue": 8,
    "盗贼": 8,
    "monk": 8,
    "武僧": 8,
    "wizard": 6,
    "法师": 6,
    "sorcerer": 6,
    "术士": 6,
}


def dnd5e_ability_modifier(score: int) -> int:
    return (score - 10) // 2


def dnd5e_proficiency_bonus(level: int) -> int:
    return 2 + (max(1, level) - 1) // 4


def dnd5e_class_hit_die(class_name: str, explicit_hit_die: int | None = None) -> int:
    """返回 DND 5E 职业生命骰。显式值(自定义职业)优先,否则匹配内置名,最后回退 d8。"""
    if explicit_hit_die is not None:
        return max(6, min(12, int(explicit_hit_die)))
    normalized = (class_name or "").strip().lower()
    matches = [
        (name, hit_die)
        for name, hit_die in DND5E_CLASS_HIT_DICE.items()
        if name in normalized
    ]
    if matches:
        return max(matches, key=lambda item: len(item[0]))[1]
    return 8


def class_record(char: dict, class_name: str | None = None) -> dict:
    """从 char -> 当前会话 -> 内置表 顺序查找职业记录。
    返回 {hit_die, primary_ability?, source}。
    """
    name = class_name if class_name else char.get("class", "")
    explicit = char.get("class_hit_die") if class_name is None else None
    if class_name is None and explicit:
        return {"hit_die": int(explicit), "source": "char"}
    hit_die = dnd5e_class_hit_die(name, explicit)
    record = {"hit_die": hit_die, "source": "builtin"}
    return record


def roll_dnd5e_ability_scores() -> tuple[dict[str, int], dict[str, list[int]]]:
    scores = {}
    rolls_by_ability = {}
    for ability in DND5E_ABILITIES:
        rolls = [random.randint(1, 6) for _ in range(4)]
        scores[ability] = sum(sorted(rolls, reverse=True)[:3])
        rolls_by_ability[ability] = rolls
    return scores, rolls_by_ability


def parse_dnd5e_ability_scores(raw: str) -> tuple[dict[str, int] | None, str | None]:
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None, "ability_scores 必须是 JSON 对象。"
    if not isinstance(parsed, dict):
        return None, "ability_scores 必须是 JSON 对象。"
    normalized = {str(k).upper(): v for k, v in parsed.items()}
    missing = [ability for ability in DND5E_ABILITIES if ability not in normalized]
    if missing:
        return None, f"缺少属性: {', '.join(missing)}。"
    scores = {}
    for ability in DND5E_ABILITIES:
        value = normalized[ability]
        if isinstance(value, bool) or not isinstance(value, int):
            return None, f"{ability} 必须是整数。"
        if value < 3 or value > 18:
            return None, f"{ability}={value} 超出开局范围 3~18。"
        scores[ability] = value
    return scores, None


def initialize_dnd5e_character(
    char: dict,
    scores: dict[str, int],
    rolls_by_ability: dict[str, list[int]] | None = None,
) -> None:
    rules = {**char.get("world_rules", {})}
    rules["stats"] = list(DND5E_ABILITIES)
    rules["auto_stat_growth"] = 0
    char["world_rules"] = rules
    char["game_system"] = "dnd5e"
    char["ability_score_rolls"] = rolls_by_ability or {}
    for ability in DND5E_ABILITIES:
        char[f"base_{ability}"] = scores[ability]
        char.pop(f"alloc_{ability}", None)
        char.pop(ability, None)
    explicit_hit_die = char.get("class_hit_die") or (
        (char.get("world_rules", {}).get("classes", {}) or {})
        .get(char.get("class", ""), {})
        .get("hit_die")
    )
    hit_die = dnd5e_class_hit_die(char.get("class", ""), explicit_hit_die)
    con_mod = dnd5e_ability_modifier(scores["CON"])
    dex_mod = dnd5e_ability_modifier(scores["DEX"])
    attack_mod = max(
        dnd5e_ability_modifier(scores["STR"]),
        dex_mod,
    )
    char["hit_die"] = hit_die
    char["max_hp"] = max(1, hit_die + con_mod)
    char["hp"] = char["max_hp"]
    char["atk"] = dnd5e_proficiency_bonus(1) + attack_mod
    char["def"] = 10 + dex_mod
    char["spd"] = 30
    char["unspent_points"] = 0


# ════════════════════════════════════════════════════════════════════
# 2. 存储层 — 角色存档 + 会话
# ════════════════════════════════════════════════════════════════════


def _char_path(data_dir: str, uid: str) -> str:
    save_dir = os.path.join(data_dir, "rpg_saves")
    os.makedirs(save_dir, exist_ok=True)
    return os.path.join(save_dir, f"{uid}.json")


def load_char(data_dir: str, uid: str) -> dict | None:
    """加载角色存档,对旧存档做惰性迁移。"""
    path = _char_path(data_dir, uid)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            char = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("rpg存档损坏 %s: %s", path, e)
        return None
    migrated = False
    if "world_rules" not in char:
        char["world_rules"] = {}
        migrated = True
    if "world" not in char:
        char["world"] = "default"
        migrated = True
    if migrated:
        save_char(data_dir, uid, char)  # 失败也无所谓,内存版本已完整
    return char


def save_char(data_dir: str, uid: str, char: dict) -> None:
    path = _char_path(data_dir, uid)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(char, f, ensure_ascii=False, indent=2)


def _session_path(data_dir: str, session_id: str) -> str:
    d = os.path.join(data_dir, "sessions")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{session_id}.json")


def purge_group_rpg_data(data_dir: str, group_id: str) -> dict:
    """删除指定群的所有 RPG 角色存档 + 该群的全部 RPG 会话文件。

    供 /创建(覆盖旧会话)与 /删除(主动清理)共用。
    返回 {"deleted_chars": int, "deleted_sessions": [session_id, ...]}。
    group_id 为空字符串时直接返回空统计(私聊无群维度)。
    """
    result = {"deleted_chars": 0, "deleted_sessions": []}
    if not group_id:
        return result

    # 1) 删除该群的所有 RPG 角色存档(按 {group_id}_ 前缀,跨 sender 清理)
    save_dir = os.path.join(data_dir, "rpg_saves")
    if os.path.exists(save_dir):
        prefix = f"{group_id}_"
        for fname in list(os.listdir(save_dir)):
            if fname.startswith(prefix) and fname.endswith(".json"):
                try:
                    os.remove(os.path.join(save_dir, fname))
                    result["deleted_chars"] += 1
                except OSError as e:
                    logger.warning("rpg存档删除失败 %s: %s", fname, e)

    # 2) 删除该群的所有 RPG 会话文件 + 它们的成员角色存档(防御性)
    sess_dir = os.path.join(data_dir, "sessions")
    if os.path.exists(sess_dir):
        for fname in list(os.listdir(sess_dir)):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(sess_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    s = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            if s.get("group_id") != group_id:
                continue
            for member_name in s.get("members", []):
                save = _char_path(data_dir, f"{group_id}_{member_name}")
                if os.path.exists(save):
                    try:
                        os.remove(save)
                    except OSError:
                        pass
            try:
                os.remove(fpath)
                result["deleted_sessions"].append(s.get("session_id", fname[:-5]))
            except OSError as e:
                logger.warning("rpg会话删除失败 %s: %s", fpath, e)

    return result


def load_session(data_dir: str, session_id: str) -> dict | None:
    p = _session_path(data_dir, session_id)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("rpg会话损坏 %s: %s", p, e)
        return None


def save_session(data_dir: str, session_id: str, data: dict) -> None:
    p = _session_path(data_dir, session_id)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ════════════════════════════════════════════════════════════════════
# 3. 数值层 — 升级 / 经验 / 属性点
# ════════════════════════════════════════════════════════════════════


def exp_needed(level: int, preset: dict) -> int:
    return int(preset["exp_base"] * (preset["exp_scale"] ** (level - 1)))


def parse_points_per_level(val) -> int | list[int]:
    """ "5" / 5 / "5-10" -> int 或 [lo, hi];无效返回 0。"""
    if isinstance(val, list) and len(val) == 2:
        return [int(val[0]), int(val[1])]
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        s = val.strip()
        if "-" in s:
            lo, _, hi = s.partition("-")
            try:
                lo_i, hi_i = int(lo.strip()), int(hi.strip())
                if lo_i > 0 and hi_i >= lo_i:
                    return [lo_i, hi_i]
            except ValueError:
                pass
        try:
            return int(s)
        except ValueError:
            pass
    return 0


def roll_points(ppl) -> int:
    """区间型撒点;常数型原样返回。"""
    if isinstance(ppl, list) and len(ppl) == 2:
        return random.randint(ppl[0], ppl[1])
    return int(ppl)


def avg_points(ppl) -> float:
    """每级属性点的期望值,用于回退 / 重算。"""
    if isinstance(ppl, list) and len(ppl) == 2:
        return (ppl[0] + ppl[1]) / 2
    return float(ppl)


def fmt_ppl(ppl) -> str:
    """属性点区间/常数显示为「5~10」或「5」。"""
    if isinstance(ppl, list) and len(ppl) == 2:
        return f"{ppl[0]}~{ppl[1]}"
    return str(ppl)


def apply_levelups(char: dict, preset: dict) -> list[int]:
    """消耗 exp,顺次触发升级,返回新达成的等级列表。原 dict 被原地修改。"""
    level_ups = []
    while char["exp"] >= exp_needed(char["level"], preset):
        cost = exp_needed(char["level"], preset)
        char["exp"] -= cost
        char["level"] += 1
        if char.get("game_system") == "dnd5e":
            con_score = int(_lookup_char_attr(char, "CON") or 10)
            hp_gain = max(
                1, char.get("hit_die", 8) // 2 + 1 + dnd5e_ability_modifier(con_score)
            )
            char["max_hp"] += hp_gain
            char["hp"] = min(char["hp"] + hp_gain, char["max_hp"])
            str_score = int(_lookup_char_attr(char, "STR") or 10)
            dex_score = int(_lookup_char_attr(char, "DEX") or 10)
            char["atk"] = dnd5e_proficiency_bonus(char["level"]) + max(
                dnd5e_ability_modifier(str_score),
                dnd5e_ability_modifier(dex_score),
            )
            if char["level"] in DND5E_ASI_LEVELS:
                char["unspent_points"] = char.get("unspent_points", 0) + 2
        else:
            char["max_hp"] += preset["hp_lv"]
            char["hp"] = min(char["hp"] + preset["hp_lv"], char["max_hp"])
            char["atk"] += preset["atk_lv"]
            char["def"] += preset["def_lv"]
            char["spd"] += preset["spd_lv"]
            auto = preset.get("auto_stat_growth", 1)
            if auto > 0:
                for stat in preset.get("stats", []):
                    base_key = f"base_{stat}"
                    char[base_key] = char.get(base_key, char.get(stat, 5)) + auto
            attr_pts = char.get(
                "attr_points_per_level",
                preset.get("attr_points_per_level", 0),
            )
            rolled = roll_points(attr_pts)
            if rolled > 0:
                char["unspent_points"] = char.get("unspent_points", 0) + rolled
        level_ups.append(char["level"])
    return level_ups


# ════════════════════════════════════════════════════════════════════
# 4. 装备层
# ════════════════════════════════════════════════════════════════════

_BUILTIN_ITEM_ATTRS = frozenset({"atk", "def", "hp", "spd"})
_OP_PRECEDENCE = (">=", "<=", "!=", ">", "<", "=")


def parse_item_attributes(attr_str: str) -> dict:
    """解析用户传入的 JSON 装备属性字符串;只保留数值。"""
    if not attr_str or not attr_str.strip():
        return {}
    try:
        d = json.loads(attr_str.strip())
        if isinstance(d, dict):
            return {str(k): v for k, v in d.items() if isinstance(v, (int, float))}
    except (json.JSONDecodeError, TypeError):
        pass
    return {}


def check_equip_conditions(char: dict, condition_str: str) -> tuple[bool, str]:
    """检查形如「level>=10,STR>=15」的条件;返回 (ok, fail_reason)。

    除内置 level/hp/max_hp/atk/def/spd 外,其余键做大小写不敏感查找,
    以兼容世界自定义属性(如 GGO 的 STR、奇幻世界的「魔力」等)。
    """
    if not condition_str or not condition_str.strip():
        return True, ""
    conds = [c.strip() for c in condition_str.split(",") if c.strip()]
    for cond in conds:
        op = next((o for o in _OP_PRECEDENCE if o in cond), None)
        if op is None:
            return False, f"无法解析条件: {cond}"
        key, _, raw_val = cond.partition(op)
        try:
            val = float(raw_val.strip())
        except ValueError:
            continue
        key = key.strip()
        char_val = _lookup_char_attr(char, key)
        if char_val is None:
            return False, f"未知属性: {key}"
        cmp_ok = {
            ">=": char_val >= val,
            "<=": char_val <= val,
            ">": char_val > val,
            "<": char_val < val,
            "=": char_val == val,
            "!=": char_val != val,
        }[op]
        if not cmp_ok:
            return False, f"{key}={char_val} 需{op}{val:g}"
    return True, ""


def _lookup_char_attr(char: dict, key: str):
    """按大小写不敏感在 char 上查找一个属性;找不到返回 None。

    优先匹配 base_<X> / alloc_<X> 形式的键,返回相加后的有效值;
    否则回退到直查字段,最后做大小写不敏感遍历。
    """
    lk = key.lower()
    if lk == "level":
        return char.get("level", 1)
    base_val = alloc_val = None
    for ck, cv in char.items():
        if not isinstance(cv, (int, float)):
            continue
        if ck.lower().startswith("base_") and ck[5:].lower() == lk:
            base_val = cv
        elif ck.lower().startswith("alloc_") and ck[6:].lower() == lk:
            alloc_val = cv
    if base_val is not None or alloc_val is not None:
        return (base_val or 0) + (alloc_val or 0)
    if key in char and isinstance(char[key], (int, float)):
        return char[key]
    for ck, cv in char.items():
        if isinstance(cv, (int, float)) and ck.lower() == lk:
            return cv
    return None


def _take_item_bonuses(char: dict, item: dict) -> None:
    """从 char 上回退一件装备的数值收益。"""
    char["atk"] = char.get("atk", 0) - item.get("ba", 0)
    char["def"] = char.get("def", 0) - item.get("bd", 0)
    char["max_hp"] = char.get("max_hp", 0) - item.get("bh", 0)
    char["hp"] = min(char.get("hp", 0), char["max_hp"])
    char["spd"] = char.get("spd", 0) - item.get("bs", 0)
    for k, v in item.get("custom", {}).items():
        char[k] = char.get(k, 0) - v


def _give_item_bonuses(
    char: dict, b_atk: int, b_def: int, b_hp: int, b_spd: int, custom: dict
) -> None:
    """把一件装备的数值收益应用到 char 上。"""
    char["atk"] = char.get("atk", 0) + b_atk
    char["def"] = char.get("def", 0) + b_def
    char["max_hp"] = char.get("max_hp", 0) + b_hp
    char["hp"] = min(char.get("hp", 0) + b_hp, char["max_hp"])
    char["spd"] = char.get("spd", 0) + b_spd
    for k, v in custom.items():
        char[k] = char.get(k, 0) + v


def _roll_random_bonuses(item_name: str) -> tuple[int, int, int, int, dict]:
    """基于装备名哈希生成稳定的随机词条。"""
    rng = random.Random(hash(item_name))
    return (
        rng.randint(0, 5),  # b_atk
        rng.randint(0, 3),  # b_def
        rng.randint(0, 20),  # b_hp
        rng.randint(-1, 2),  # b_spd
        {},  # custom
    )


# ════════════════════════════════════════════════════════════════════
# 5. 显示层
# ════════════════════════════════════════════════════════════════════


def _bar(cur: int, mx: int, length: int = 10) -> str:
    ratio = max(0, min(1, cur / mx)) if mx > 0 else 0
    filled = int(ratio * length)
    return "█" * filled + "░" * (length - filled)


def _fmt_stat_line(stat: str, char: dict) -> str | None:
    base = char.get(f"base_{stat}", char.get(stat))
    if base is None:
        return None
    alloc = char.get(f"alloc_{stat}", 0)
    total = base + alloc
    modifier = ""
    if char.get("game_system") == "dnd5e" and stat.upper() in DND5E_ABILITIES:
        modifier = f" ({dnd5e_ability_modifier(total):+d})"
    if alloc > 0:
        return f"  {stat}: {total}{modifier} (基础{base}+{alloc})"
    return f"  {stat}: {total}{modifier}"


def _format_status(char: dict) -> str:
    """角色的富文本状态卡(LLM 直接展示给用户)。"""
    preset = {**DEFAULT_WORLD_RULES, **char.get("world_rules", {})}
    stats = preset.get("stats", [])
    exp_max = exp_needed(char["level"], preset)
    session_info = f" | 会话: {char['session_id']}" if char.get("session_id") else ""

    combat_line = f"⚔ ATK: {char['atk']}  🛡 DEF: {char['def']}  💨 SPD: {char['spd']}"
    if char.get("game_system") == "dnd5e":
        combat_line = (
            f"⚔ 攻击加值: {char['atk']:+d}  🛡 AC: {char['def']}  "
            f"💨 移速: {char['spd']}尺"
        )

    lines = [
        f"━━ {char['name']} ━━",
        f"职业: {char.get('class', '无')} | Lv.{char['level']} | 世界: {char['world']}{session_info}",
        "",
        f"HP  {_bar(char['hp'], char['max_hp'])} {char['hp']}/{char['max_hp']}",
        f"EXP {_bar(char['exp'], exp_max)} {char['exp']}/{exp_max}",
        "",
        combat_line,
        f"💰 货币: {char.get('currency', 0)}",
    ]

    # 世界定义的额外属性块(默认空 — 没有就不渲染)
    stat_lines = [s for s in (_fmt_stat_line(n, char) for n in stats) if s]
    if stat_lines:
        lines.append("")
        lines.extend(stat_lines)

    # 自定义数值属性 — 排除内置键 + 世界属性的所有衍生键(base_/alloc_/原名/小写)
    skip: set[str] = set()
    for s in stats:
        skip.update(
            {
                s,
                s.lower(),
                f"base_{s}",
                f"base_{s.lower()}",
                f"alloc_{s}",
                f"alloc_{s.lower()}",
            }
        )
    custom = {
        k: v
        for k, v in char.items()
        if k not in BUILTIN_CHAR_KEYS and k not in skip and isinstance(v, (int, float))
    }
    if custom:
        for k, v in custom.items():
            lines.append(f"  {k}: {v}")

    # 未分配属性点
    pts = char.get("unspent_points", 0)
    if pts > 0:
        ppt_lv = char.get("attr_points_per_level", 0)
        suffix = f" (每级+{fmt_ppl(ppt_lv)})" if ppt_lv else ""
        lines.append(f"\n⚡ 未分配属性点: {pts}{suffix}")
    elif char.get("attr_points_per_level"):
        lines.append(f"\n⚡ 每级属性点: +{fmt_ppl(char['attr_points_per_level'])}")

    # 装备
    eq = char.get("equipment", {})
    if eq:
        lines.append("")
        lines.append("【装备】")
        for slot, item in eq.items():
            sname = SLOT_CN.get(slot, slot)
            cond = f" [需: {item['condition']}]" if item.get("condition") else ""
            effect = f" ✦{item['special_effect']}" if item.get("special_effect") else ""
            lines.append(
                f"  {sname}: {item['name']} ({item.get('desc', '')}){effect}{cond}"
            )

    # 技能 / 背包 / 增益减益
    skills = char.get("skills", [])
    if skills:
        lines.append(f"\n【技能】{', '.join(skills)}")
    inv = char.get("inventory", [])
    if inv:
        counts = {}
        for it in inv:
            counts[it] = counts.get(it, 0) + 1
        lines.append("【背包】")
        for it, cnt in counts.items():
            lines.append(f"  · {it}" + (f" ×{cnt}" if cnt > 1 else ""))
    if char.get("buffs"):
        lines.append(f"【增益】{', '.join(char['buffs'])}")
    if char.get("debuffs"):
        lines.append(f"【减益】{', '.join(char['debuffs'])}")

    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════
# 6. RPGMixin — LLM 工具
# ════════════════════════════════════════════════════════════════════


class RPGMixin:
    """RPG 工具 mixin。需要主插件在 __init__ 里设置 self.data_dir = StarTools.get_data_dir()。"""

    # ─────────────── 私有助手 ───────────────

    def _uid(self, event) -> str:
        return str(event.get_sender_id())

    def _get_group_id(self, event) -> str:
        try:
            gid = ""
            if hasattr(event, "get_group_id"):
                gid = str(event.get_group_id() or "")
            elif hasattr(event, "message_obj") and hasattr(
                event.message_obj, "group_id"
            ):
                gid = str(event.message_obj.group_id or "")
            return gid if gid.isdigit() else ""
        except Exception:
            return ""

    def _make_char_uid(self, group_id: str, char_name: str, sender_uid: str) -> str:
        return f"{group_id}_{char_name}" if group_id else sender_uid

    def _resolve_uid(self, event, target: str) -> str:
        """把 LLM 传入的「角色名」解析成存档 uid。"""
        sender_uid = self._uid(event)
        group_id = self._get_group_id(event)
        candidates = []
        if group_id:
            candidates.append(self._make_char_uid(group_id, target, sender_uid))
        candidates.append(target)
        for c in candidates:
            if load_char(self.data_dir, c):
                return c
        # 退回到当前用户的会话上下文
        char = load_char(self.data_dir, sender_uid)
        session_id = char.get("session_id", "") if char else ""
        if session_id:
            session = load_session(self.data_dir, session_id)
            if session and target in session.get("members", []):
                return self._make_char_uid(group_id, target, sender_uid)
        return candidates[0]

    def _world_preset(self, char: dict) -> dict:
        return {**DEFAULT_WORLD_RULES, **char.get("world_rules", {})}

    def _require_char(
        self, event, target: str
    ) -> tuple[str | None, dict | None, str | None]:
        """统一处理「解析 uid → 读存档」,失败直接返回错误消息。"""
        uid = self._resolve_uid(event, target)
        char = load_char(self.data_dir, uid)
        if not char:
            return uid, None, "❌ 还没有角色,请先用 rpg_join_session 创建。"
        return uid, char, None

    def _persist(self, uid: str, char: dict) -> None:
        save_char(self.data_dir, uid, char)

    # ─────────────── 数值快照 / 回滚(给 /undo 用) ───────────────

    def _rpg_snapshot(self, event, mode: str) -> dict:
        """抓取当前 RPG 数值快照(角色存档 + 会话文件),供 /undo 回滚。

        mode A(纯叙事)没有 RPG 状态,直接返回空快照。
        群聊 scope = `{group_id}_*.json` 全部角色 + 同 group_id 的全部 session。
        私聊 scope = 当前 sender 的存档 + 该存档引用的 session(避免误删别人的私聊存档)。
        """
        if mode not in ("B", "C"):
            return {"scope": {"group_id": "", "sender_uid": ""}, "chars": {}, "sessions": {}}

        group_id = self._get_group_id(event)
        sender_uid = self._uid(event)
        chars: dict[str, dict] = {}
        session_ids: set[str] = set()

        save_dir = os.path.join(self.data_dir, "rpg_saves")
        if os.path.exists(save_dir):
            if group_id:
                prefix = f"{group_id}_"
                for fname in os.listdir(save_dir):
                    if fname.startswith(prefix) and fname.endswith(".json"):
                        uid = fname[:-5]
                        char = load_char(self.data_dir, uid)
                        if char is not None:
                            chars[uid] = char
            else:
                char = load_char(self.data_dir, sender_uid)
                if char is not None:
                    chars[sender_uid] = char

        sess_dir = os.path.join(self.data_dir, "sessions")
        if os.path.exists(sess_dir):
            if group_id:
                for fname in os.listdir(sess_dir):
                    if not fname.endswith(".json"):
                        continue
                    sid = fname[:-5]
                    try:
                        with open(os.path.join(sess_dir, fname), "r", encoding="utf-8") as f:
                            s = json.load(f)
                    except (OSError, json.JSONDecodeError):
                        continue
                    if s.get("group_id") == group_id:
                        session_ids.add(sid)
            else:
                for char in chars.values():
                    sid = char.get("session_id")
                    if sid:
                        session_ids.add(sid)

        sessions: dict[str, dict] = {}
        for sid in session_ids:
            s = load_session(self.data_dir, sid)
            if s is not None:
                sessions[sid] = s

        return {
            "scope": {"group_id": group_id, "sender_uid": sender_uid},
            "chars": chars,
            "sessions": sessions,
        }

    def _rpg_restore(self, snapshot: dict) -> dict:
        """把 RPG 状态回滚到 snapshot。

        - snapshot 中存在 → 写回磁盘
        - 当前磁盘上存在但 snapshot 不存在(被回滚期间创建)→ 删除
        返回 {"restored_chars": int, "restored_sessions": int,
              "deleted_chars": int, "deleted_sessions": int}。
        """
        scope = snapshot.get("scope") or {}
        group_id = scope.get("group_id", "") or ""
        sender_uid = scope.get("sender_uid", "") or ""
        chars_snap: dict = snapshot.get("chars") or {}
        sessions_snap: dict = snapshot.get("sessions") or {}

        stats = {
            "restored_chars": 0,
            "restored_sessions": 0,
            "deleted_chars": 0,
            "deleted_sessions": 0,
        }

        save_dir = os.path.join(self.data_dir, "rpg_saves")
        os.makedirs(save_dir, exist_ok=True)

        current_uids: set[str] = set()
        if group_id:
            prefix = f"{group_id}_"
            if os.path.exists(save_dir):
                for fname in os.listdir(save_dir):
                    if fname.startswith(prefix) and fname.endswith(".json"):
                        current_uids.add(fname[:-5])
        else:
            path = os.path.join(save_dir, f"{sender_uid}.json")
            if os.path.exists(path):
                current_uids.add(sender_uid)

        for uid in current_uids:
            if uid in chars_snap:
                save_char(self.data_dir, uid, chars_snap[uid])
                stats["restored_chars"] += 1
            else:
                path = os.path.join(save_dir, f"{uid}.json")
                try:
                    os.remove(path)
                    stats["deleted_chars"] += 1
                except OSError as e:
                    logger.debug("rpg 回滚删除角色失败 %s: %s", uid, e)

        for uid, char in chars_snap.items():
            if uid not in current_uids:
                save_char(self.data_dir, uid, char)
                stats["restored_chars"] += 1

        sess_dir = os.path.join(self.data_dir, "sessions")
        os.makedirs(sess_dir, exist_ok=True)

        current_sids: set[str] = set()
        snap_sids = set(sessions_snap.keys())
        if os.path.exists(sess_dir):
            for fname in os.listdir(sess_dir):
                if not fname.endswith(".json"):
                    continue
                sid = fname[:-5]
                s = load_session(self.data_dir, sid)
                if s is None:
                    continue
                if group_id:
                    if s.get("group_id") == group_id:
                        current_sids.add(sid)
                else:
                    if (not s.get("group_id")) and sid in snap_sids:
                        current_sids.add(sid)

        for sid in current_sids:
            if sid in sessions_snap:
                save_session(self.data_dir, sid, sessions_snap[sid])
                stats["restored_sessions"] += 1
            else:
                path = os.path.join(sess_dir, f"{sid}.json")
                try:
                    os.remove(path)
                    stats["deleted_sessions"] += 1
                except OSError as e:
                    logger.debug("rpg 回滚删除会话失败 %s: %s", sid, e)

        for sid, s in sessions_snap.items():
            if sid not in current_sids:
                save_session(self.data_dir, sid, s)
                stats["restored_sessions"] += 1

        return stats

    # ─────────────── 会话管理 ───────────────

    @filter.llm_tool(name="rpg_create_session")
    async def rpg_create_session(
        self,
        event,
        world: str,
        points_per_level: str = "",
        world_rules: str = "",
        game_system: str = "",
    ) -> str:
        """
        Create a game session with a custom world. All characters joining this session will inherit the world's growth rules. Do this BEFORE creating characters.
        ⚠️ 群聊模式:一个群只能有一个会话!再次创建会覆盖旧会话并删除所有角色数据!

        Args:
            world(string): World name (any string the user chooses, e.g. "我的奇幻世界", "末日废土", "赛博朋克"). Just a label.
            points_per_level(string): Optional. Custom attribute points per level-up. Fixed number (e.g. "5") or range (e.g. "5-10"). Empty string = use DEFAULT_WORLD_RULES (0 by default).
            world_rules(string): Optional. JSON string of custom growth rules overriding DEFAULT_WORLD_RULES. Supports numeric growth keys and a stats string list. Example: '{"base_hp":120,"hp_lv":20,"stats":["STR","DEX"]}'. Empty = use DEFAULT_WORLD_RULES.
            game_system(string): Optional. Set to "dnd5e" for DND 5E. In mode C it is selected automatically. DND characters always receive persisted STR/DEX/CON/INT/WIS/CHA scores.
        Returns:
            Session ID and config summary.
        """
        group_id = self._get_group_id(event)
        overwritten = False
        old_session_name = ""
        game_system = (game_system or "").strip().lower()
        if not game_system and hasattr(self, "_load_sim"):
            try:
                sim_session = await self._load_sim(event)
                if sim_session and sim_session.get("mode") == "C":
                    game_system = "dnd5e"
            except Exception:
                pass
        if game_system not in ("", "dnd5e"):
            return "❌ game_system 仅支持空值或 dnd5e。"

        # 群聊清理旧会话及其角色
        if group_id:
            purge = purge_group_rpg_data(self.data_dir, group_id)
            overwritten = bool(purge["deleted_chars"] or purge["deleted_sessions"])
            if purge["deleted_sessions"]:
                old_session_name = purge["deleted_sessions"][0]

        user_rules: dict = {}
        if world_rules and world_rules.strip():
            try:
                parsed = json.loads(world_rules.strip())
                if isinstance(parsed, dict):
                    for key, value in parsed.items():
                        key = str(key)
                        if key == "stats" and isinstance(value, list):
                            stats = [
                                str(stat).strip() for stat in value if str(stat).strip()
                            ]
                            if stats:
                                user_rules[key] = stats
                        elif not isinstance(value, bool) and isinstance(
                            value, (int, float)
                        ):
                            user_rules[key] = value
            except (json.JSONDecodeError, TypeError):
                pass

        if game_system == "dnd5e":
            user_rules["stats"] = list(DND5E_ABILITIES)
            user_rules["auto_stat_growth"] = 0

        preset = {**DEFAULT_WORLD_RULES, **user_rules}
        ppl = parse_points_per_level(points_per_level) if points_per_level else 0
        if game_system == "dnd5e":
            ppl = 0
        elif not ppl:
            ppl = preset.get("attr_points_per_level", 0)

        session_id = f"s_{int(time.time())}_{random.randint(100, 999)}"
        session = {
            "session_id": session_id,
            "world": world,
            "game_system": game_system,
            "world_rules": user_rules,
            "attr_points_per_level": ppl,
            "members": [],
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "group_id": group_id,
        }
        save_session(self.data_dir, session_id, session)

        lines = []
        if overwritten:
            lines += [
                f"⚠️ 已覆盖旧会话「{old_session_name}」",
                "   旧会话的所有角色数据已清除!",
                "",
            ]
        rules_str = (
            "(使用默认)"
            if not user_rules
            else ", ".join(f"{k}={v}" for k, v in sorted(user_rules.items()))
        )
        lines += [
            "🌍 会话创建成功",
            f"  ID: {session_id}",
            f"  世界: {world}",
            f"  规则系统: {game_system or '通用 RPG'}",
            f"  每级属性点: {fmt_ppl(ppl)}",
            f"  自定义成长规则: {rules_str}",
            "  成员: 无",
            "",
            "用 rpg_join_session 让角色加入吧。",
        ]
        return "\n".join(lines)

    @filter.llm_tool(name="rpg_join_session")
    async def rpg_join_session(
        self,
        event,
        session_id: str,
        name: str,
        character_class: str = "",
        ability_scores: str = "",
    ) -> str:
        """
        Create a character and join an existing game session. DND 5E sessions automatically roll and persist all six ability scores with 4d6kh3 when ability_scores is empty.
        Args:
            session_id(string): The session ID returned by rpg_create_session.
            name(string): Character name.
            character_class(string): Optional. Character class/job. If empty or invalid, a random class from the world preset is chosen.
            ability_scores(string): Optional for DND 5E. JSON with all six scores, e.g. '{"STR":15,"DEX":14,"CON":13,"INT":12,"WIS":10,"CHA":8}'. Every score must be 3~18. Empty = automatically roll each score with 4d6kh3.
        Returns:
            Character status sheet with session info and DND ability roll details.
        """
        sender_uid = self._uid(event)
        group_id = self._get_group_id(event)
        uid = self._make_char_uid(group_id, name, sender_uid)

        session = load_session(self.data_dir, session_id)
        if not session:
            return f"❌ 会话 {session_id} 不存在。"

        preset = {**DEFAULT_WORLD_RULES, **session.get("world_rules", {})}
        classes_dict = preset.get("classes") or {}

        if not character_class or character_class not in classes_dict:
            character_class = (
                random.choice(list(classes_dict))
                if classes_dict
                else (character_class.strip() or "冒险者")
            )
        cb = classes_dict.get(character_class, {})
        game_system = (session.get("game_system") or "").strip().lower()
        if ability_scores and not game_system:
            return "❌ ability_scores 仅适用于 game_system=dnd5e 的会话。"

        scores = None
        rolls_by_ability = None
        if game_system == "dnd5e":
            if ability_scores and ability_scores.strip():
                scores, score_error = parse_dnd5e_ability_scores(ability_scores.strip())
                if score_error:
                    return f"❌ DND 5E 六维属性无效: {score_error}"
            else:
                scores, rolls_by_ability = roll_dnd5e_ability_scores()

        char = self._new_character(
            name=name,
            world=session["world"],
            class_name=character_class,
            class_bonus=cb,
            preset=preset,
            session_id=session_id,
            world_rules=session.get("world_rules", {}),
            game_system=game_system,
        )
        if scores is not None:
            initialize_dnd5e_character(char, scores, rolls_by_ability)
        ppl = session.get("attr_points_per_level", 0)
        if ppl and game_system != "dnd5e":
            char["attr_points_per_level"] = ppl

        if name not in session["members"]:
            session["members"].append(name)
        save_session(self.data_dir, session_id, session)
        self._persist(uid, char)
        save_info = f" (存档ID: {uid})" if group_id else ""
        lines = [f"✅ {name} 加入会话「{session_id}」{save_info}"]
        if scores is not None:
            title = (
                "🎲 DND 5E 六维属性(4d6kh3,每项取最高三骰)"
                if rolls_by_ability
                else "🎲 DND 5E 六维属性(指定值)"
            )
            lines += ["", title]
            for ability in DND5E_ABILITIES:
                score = scores[ability]
                modifier = dnd5e_ability_modifier(score)
                if rolls_by_ability:
                    rolls = rolls_by_ability[ability]
                    dropped = min(rolls)
                    detail = f"{rolls} 弃 {dropped}"
                    lines.append(f"  {ability}: {detail} = {score} ({modifier:+d})")
                else:
                    lines.append(f"  {ability}: {score} ({modifier:+d})")
        lines += ["", _format_status(char)]
        return "\n".join(lines)

    @staticmethod
    def _new_character(
        name: str,
        world: str,
        class_name: str,
        class_bonus: dict,
        preset: dict,
        session_id: str,
        world_rules: dict,
        game_system: str = "",
    ) -> dict:
        char = {
            "name": name,
            "world": world,
            "world_rules": world_rules,
            "game_system": game_system,
            "session_id": session_id,
            "class": class_name,
            "level": 1,
            "exp": 0,
            "hp": preset["base_hp"] + class_bonus.get("hp", 0),
            "max_hp": preset["base_hp"] + class_bonus.get("hp", 0),
            "atk": preset["base_atk"] + class_bonus.get("atk", 0),
            "def": preset["base_def"] + class_bonus.get("def", 0),
            "spd": preset["base_spd"] + class_bonus.get("spd", 0),
            "skills": list(preset.get("skills", [])),
            "equipment": {},
            "buffs": [],
            "debuffs": [],
            "inventory": [],
            "currency": 0,
            "kills": 0,
        }
        for attr, val in class_bonus.get("custom", {}).items():
            char[attr] = val
        return char

    @filter.llm_tool(name="rpg_list_sessions")
    async def rpg_list_sessions(self, event) -> str:
        """
        List all active game sessions.
        Returns:
            List of sessions with their configs and member counts.
        """
        sd = os.path.join(self.data_dir, "sessions")
        if not os.path.exists(sd):
            return "暂无会话。用 rpg_create_session 创建一个吧。"
        files = [f for f in os.listdir(sd) if f.endswith(".json")]
        if not files:
            return "暂无会话。用 rpg_create_session 创建一个吧。"

        lines = ["📋 活跃会话:"]
        for fname in sorted(files):
            try:
                with open(os.path.join(sd, fname), "r", encoding="utf-8") as fh:
                    s = json.load(fh)
            except (json.JSONDecodeError, OSError):
                continue
            members = ", ".join(s.get("members", [])) or "无"
            lines += [
                f"  [{s['session_id']}]",
                f"    世界: {s['world']} | 每级点数: {fmt_ppl(s.get('attr_points_per_level', 0))}",
                f"    成员: {members}",
            ]
        return "\n".join(lines)

    @filter.llm_tool(name="rpg_list_members")
    async def rpg_list_members(self, event, session_id: str) -> str:
        """
        List all members in a game session with their class and level info (looked up from character saves).
        Args:
            session_id(string): The session ID to query.
        Returns:
            List of members with their character info.
        """
        session = load_session(self.data_dir, session_id)
        if not session:
            return f"❌ 会话 {session_id} 不存在。"
        members = session.get("members", [])
        if not members:
            return f"ℹ️ 会话「{session.get('session_id', session_id)}」暂无成员。"

        group_id = self._get_group_id(event)
        lines = [
            f"👥 会话「{session.get('session_id', session_id)}」成员 ({len(members)}人)",
            "",
        ]
        for name in members:
            uid = self._make_char_uid(group_id, name, self._uid(event))
            char = load_char(self.data_dir, uid)
            if char:
                lines.append(
                    f"  · {name} — {char.get('class', '无')} Lv.{char.get('level', 1)}"
                    f" HP:{char.get('hp', 0)}/{char.get('max_hp', 0)}"
                )
            else:
                lines.append(f"  · {name} — (存档未找到)")
        return "\n".join(lines)

    @filter.llm_tool(name="rpg_delete_session")
    async def rpg_delete_session(
        self,
        event,
        session_id: str,
    ) -> str:
        """
        Delete a game session and optionally reset all its characters. This is irreversible.
        Args:
            session_id(string): The session ID to delete (from rpg_list_sessions).
        Returns:
            Deletion confirmation with details.
        """
        session = load_session(self.data_dir, session_id)
        if not session:
            return f"❌ 会话 {session_id} 不存在。"
        members = session.get("members")

        deleted_chars = []
        if members:
            group_id = self._get_group_id(event)
            for m in members:
                path = _char_path(
                    self.data_dir, self._make_char_uid(group_id, m, self._uid(event))
                )
                if os.path.exists(path):
                    os.remove(path)
                    deleted_chars.append(m)

        p = _session_path(self.data_dir, session_id)
        if os.path.exists(p):
            os.remove(p)

        lines = [f"🗑 会话 ({session_id}) 已删除"]
        if deleted_chars:
            lines.append(f"   已删除角色: {', '.join(deleted_chars)}")
        else:
            lines.append("   无角色存档需要清理")
        lines += ["", "✅ 清理完毕,可以重新开始了。"]
        return "\n".join(lines)

    # ─────────────── 角色操作 ───────────────

    @filter.llm_tool(name="rpg_define_class")
    async def rpg_define_class(
        self,
        event,
        target: str,
        class_name: str,
        hit_die: str = "",
        primary_ability: str = "",
        custom_bonuses: str = "",
        description: str = "",
    ) -> str:
        """
        Define a custom class (non-traditional DND or general RPG) and persist it to the active session. Use this for homebrew classes, special jobs ("星见", "魔剑士", "调律师"), or any class not in the standard list. Class can later be applied with rpg_change_class.
        Args:
            target(string): Required. The character name (must belong to an active session); the class is created in that character's session.
            class_name(string): The custom class name (e.g. "魔剑士", "机魂使", "Astrologian"). Will overwrite an existing class with the same name.
            hit_die(string): Optional. DND 5E hit die: "d6", "d8", "d10", or "d12". Defaults to "d8" if omitted/invalid. Ignored for non-DND sessions.
            primary_ability(string): Optional. Primary ability label (only stored as metadata). e.g. "STR or INT", "DEX+WIS". Free text — purely informational.
            custom_bonuses(string): Optional. JSON object of stat bonuses applied at class assignment time. For generic RPG: keys like "hp","atk","def","spd","mana","faith". For DND 5E: not used (hits are determined by hit_die + CON). Example: "{"hp":15,"atk":3,"mana":50}".
            description(string): Optional. Flavor / mechanical description (free text, displayed back).
        Returns:
            Confirmation that the class was registered, plus summary and any active session members.
        """
        _, char, err = self._require_char(event, target)
        if err:
            return err
        class_name = (class_name or "").strip()
        if not class_name:
            return "❌ class_name 不能为空。"
        session_id = char.get("session_id", "")
        if not session_id:
            return "❌ 角色未加入会话,无法定义职业。"
        session = load_session(self.data_dir, session_id)
        if not session:
            return f"❌ 会话 {session_id} 不存在。"

        parsed_bonuses: dict = {}
        if custom_bonuses and custom_bonuses.strip():
            try:
                raw = json.loads(custom_bonuses.strip())
                if isinstance(raw, dict):
                    parsed_bonuses = {
                        str(k): v
                        for k, v in raw.items()
                        if not isinstance(v, bool) and isinstance(v, (int, float))
                    }
            except (json.JSONDecodeError, TypeError):
                return '❌ custom_bonuses 必须是 JSON 对象(如 {"hp":15,"atk":3})。'

        hit_die_value = None
        if hit_die and hit_die.strip():
            m = re.match(r"^\s*d\s*([0-9]+)\s*$", hit_die.strip(), re.IGNORECASE)
            if m:
                hit_die_value = max(6, min(12, int(m.group(1))))
            else:
                return "❌ hit_die 必须是 d6 / d8 / d10 / d12 之一。"

        game_system = char.get("game_system", "")
        if game_system == "dnd5e" and hit_die_value is None:
            hit_die_value = 8

        world_rules = session.get("world_rules", {}) or {}
        classes = world_rules.get("classes") or {}
        record = {"description": description.strip()} if description.strip() else {}
        if game_system == "dnd5e" and hit_die_value is not None:
            record["hit_die"] = hit_die_value
        if primary_ability.strip():
            record["primary_ability"] = primary_ability.strip()
        if parsed_bonuses:
            for k, v in parsed_bonuses.items():
                if k in ("hp", "atk", "def", "spd"):
                    record[k] = int(v)
                else:
                    record.setdefault("custom", {})[k] = int(v)
        classes[class_name] = record
        world_rules["classes"] = classes
        session["world_rules"] = world_rules
        save_session(self.data_dir, session_id, session)

        group_id = self._get_group_id(event)
        sender_uid = self._uid(event)
        propagated = []
        for member_name in session.get("members", []):
            m_uid = self._make_char_uid(group_id, member_name, sender_uid)
            m_char = load_char(self.data_dir, m_uid)
            if not m_char:
                continue
            m_rules = m_char.get("world_rules") or {}
            if not m_rules:
                m_rules = dict(session.get("world_rules", {}))
            m_classes = dict(m_rules.get("classes") or {})
            m_classes[class_name] = record
            m_rules["classes"] = m_classes
            m_char["world_rules"] = m_rules
            if m_char.get("class") == class_name:
                m_char["class_hit_die"] = (
                    hit_die_value if hit_die_value else m_char.get("class_hit_die")
                )
            save_char(self.data_dir, m_uid, m_char)
            propagated.append(member_name)

        lines = [
            f"✅ 自定义职业「{class_name}」已注册到会话「{session_id}」",
            f"   系统: {game_system or '通用 RPG'}",
        ]
        if game_system == "dnd5e":
            lines.append(f"   生命骰: d{hit_die_value}")
        if primary_ability.strip():
            lines.append(f"   主属性: {primary_ability.strip()}")
        if parsed_bonuses:
            bonus_str = ", ".join(f"{k}+{v}" for k, v in parsed_bonuses.items())
            lines.append(f"   初始加成: {bonus_str}")
        if description.strip():
            lines.append(f"   说明: {description.strip()}")
        if propagated:
            lines.append(
                f"   已同步到 {len(propagated)} 名角色: {', '.join(propagated)}"
            )
        lines += [
            "",
            '💡 用 rpg_change_class 把角色转职为该职业,或新角色加入时直接传 character_class="'
            + class_name
            + '"。',
        ]
        return "\n".join(lines)

    @filter.llm_tool(name="rpg_change_class")
    @filter.llm_tool(name="rpg_change_class")
    async def rpg_change_class(
        self,
        event,
        target: str,
        new_class: str,
        refund_points: bool = True,
    ) -> str:
        """
        Change the character's class. Can be used mid-game for class changes, or to assign a class to a character that started without one.
        Args:
            new_class(string): The new class name. If the world preset has this class, its bonuses apply. Otherwise it's treated as a custom class with no stat bonuses.
            refund_points(bool): If true, reset base stats to Lv.1 base and refund all level-up stat gains as unspent points (like a full respec). If false, only change the class label and apply new class bonuses on top.
            target(string): Required. The character name to operate on (e.g. "Kirito").
        Returns:
            Class change result with updated status.
        """
        uid, char, err = self._require_char(event, target)
        if err:
            return err

        preset = self._world_preset(char)
        old_class = char.get("class", "无")
        old_cb = preset["classes"].get(old_class, {})
        new_cb = preset["classes"].get(new_class, {})

        if char.get("game_system") == "dnd5e":
            explicit_new_hit_die = (
                new_cb.get("hit_die") if isinstance(new_cb, dict) else None
            )
            new_hit_die = dnd5e_class_hit_die(new_class, explicit_new_hit_die)
            con_score = int(_lookup_char_attr(char, "CON") or 10)
            con_mod = dnd5e_ability_modifier(con_score)
            level1_hp = max(1, new_hit_die + con_mod)
            avg_gain = max(1, new_hit_die // 2 + 1 + con_mod)
            new_max_hp = level1_hp + avg_gain * max(0, char["level"] - 1)
            char["hit_die"] = new_hit_die
            char["max_hp"] = new_max_hp
            char["hp"] = min(char["hp"], new_max_hp)
            str_score = int(_lookup_char_attr(char, "STR") or 10)
            dex_score = int(_lookup_char_attr(char, "DEX") or 10)
            char["atk"] = dnd5e_proficiency_bonus(char["level"]) + max(
                dnd5e_ability_modifier(str_score),
                dnd5e_ability_modifier(dex_score),
            )
            char["def"] = 10 + dnd5e_ability_modifier(dex_score)
            char["spd"] = 30
            char["class"] = new_class
            self._persist(uid, char)
            return f"🔄 职业变更: {old_class} → {new_class}(DND 5E HP/攻击/AC 已按新职业重算)\n\n{_format_status(char)}"

        if refund_points:
            char["max_hp"] = preset["base_hp"] + new_cb.get("hp", 0)
            char["hp"] = min(char["hp"], char["max_hp"])
            char["atk"] = preset["base_atk"] + new_cb.get("atk", 0)
            char["def"] = preset["base_def"] + new_cb.get("def", 0)
            char["spd"] = preset["base_spd"] + new_cb.get("spd", 0)
            attr_pts_raw = char.get(
                "attr_points_per_level", preset.get("attr_points_per_level", 0)
            )
            char["unspent_points"] = char.get("unspent_points", 0) + int(
                (char["level"] - 1) * avg_points(attr_pts_raw)
            )
            for attr in old_cb.get("custom", {}):
                char.pop(attr, None)
            for attr, val in new_cb.get("custom", {}).items():
                char[attr] = val
        else:
            for attr in ("hp", "atk", "def", "spd"):
                diff = new_cb.get(attr, 0) - old_cb.get(attr, 0)
                char[attr] = char.get(attr, 0) + diff
                if attr == "hp":
                    char["max_hp"] = char.get("max_hp", 0) + diff
                    char["hp"] = min(char["hp"], char["max_hp"])
            old_custom = old_cb.get("custom", {})
            new_custom = new_cb.get("custom", {})
            for attr in set(old_custom) | set(new_custom):
                ov, nv = old_custom.get(attr, 0), new_custom.get(attr, 0)
                char[attr] = char.get(attr, ov) - ov + nv

        char["class"] = new_class
        self._persist(uid, char)
        mode_str = "(重置模式)" if refund_points else "(叠加模式)"
        return f"🔄 职业变更{mode_str}: {old_class} → {new_class}\n\n{_format_status(char)}"

    @filter.llm_tool(name="rpg_get_status")
    async def rpg_get_status(self, event, target: str) -> str:
        """
        Get the current character's full status sheet including HP, EXP, stats, equipment, skills and buffs/debuffs.
        Args:
            target(string): Required. The character name to operate on (e.g. "Kirito").
        Returns:
            Formatted character status text, or error message if no character exists.
        """
        _, char, err = self._require_char(event, target)
        if err:
            return err
        return _format_status(char)

    @filter.llm_tool(name="rpg_set_level")
    async def rpg_set_level(self, event, target: str, target_level: int) -> str:
        """
        Directly set the character's level. Stats grow automatically based on the world preset. If the world uses attr_points_per_level, unspent_points is correctly adjusted to match the new level.
        Args:
            target(string): Required. The character name to operate on (e.g. "Kirito").
            target_level(int): The target level to set (minimum 1).
        Returns:
            Level change result with stat growth summary.
        """
        uid, char, err = self._require_char(event, target)
        if err:
            return err

        target_level = max(1, target_level)
        old_level = char["level"]
        if target_level == old_level:
            return f"ℹ️ 已经是 Lv.{old_level} 了。"

        diff = target_level - old_level
        preset = self._world_preset(char)
        pts_per_lv = 0.0
        attr_pts_raw = 0
        if char.get("game_system") == "dnd5e":
            con_score = int(_lookup_char_attr(char, "CON") or 10)
            hp_per_level = max(
                1,
                char.get("hit_die", 8) // 2 + 1 + dnd5e_ability_modifier(con_score),
            )
            hp_delta = hp_per_level * diff
            char["max_hp"] = max(1, char["max_hp"] + hp_delta)
            char["hp"] = max(1, min(char["hp"] + hp_delta, char["max_hp"]))
            old_asi_points = 2 * sum(level <= old_level for level in DND5E_ASI_LEVELS)
            new_asi_points = 2 * sum(
                level <= target_level for level in DND5E_ASI_LEVELS
            )
            spent = max(0, old_asi_points - char.get("unspent_points", 0))
            char["unspent_points"] = max(0, new_asi_points - spent)
            str_score = int(_lookup_char_attr(char, "STR") or 10)
            dex_score = int(_lookup_char_attr(char, "DEX") or 10)
            char["atk"] = dnd5e_proficiency_bonus(target_level) + max(
                dnd5e_ability_modifier(str_score),
                dnd5e_ability_modifier(dex_score),
            )
            stats_delta = (
                f"HP {hp_delta:+d}  熟练加值 {dnd5e_proficiency_bonus(target_level):+d}"
            )
        else:
            char["max_hp"] += preset["hp_lv"] * diff
            char["hp"] = max(
                1, min(char["hp"] + preset["hp_lv"] * diff, char["max_hp"])
            )
            char["max_hp"] = max(1, char["max_hp"])
            char["atk"] += preset["atk_lv"] * diff
            char["def"] += preset["def_lv"] * diff
            char["spd"] += preset["spd_lv"] * diff
            attr_pts_raw = char.get(
                "attr_points_per_level", preset.get("attr_points_per_level", 0)
            )
            pts_per_lv = avg_points(attr_pts_raw)
            if pts_per_lv > 0:
                old_pts = int((old_level - 1) * pts_per_lv)
                new_pts = int((target_level - 1) * pts_per_lv)
                spent = old_pts - char.get("unspent_points", 0)
                char["unspent_points"] = max(0, new_pts - spent)
            stats_delta = (
                f"HP {preset['hp_lv'] * diff:+d}  ATK {preset['atk_lv'] * diff:+d}"
                f"  DEF {preset['def_lv'] * diff:+d}  SPD {preset['spd_lv'] * diff:+d}"
            )

        char["level"] = target_level
        char["exp"] = 0
        self._persist(uid, char)

        sign = "⬆️" if diff > 0 else "⬇️"
        msg = f"{sign} Lv.{old_level} → Lv.{target_level}\n📊 {stats_delta}"
        if char.get("game_system") == "dnd5e":
            msg += f"\n⚡ ASI 剩余属性点: {char.get('unspent_points', 0)}"
        elif pts_per_lv > 0:
            msg += (
                f"\n⚡ 属性点 +{int(pts_per_lv * diff)} → "
                f"剩余 {char.get('unspent_points', 0)} "
                f"(每级 {fmt_ppl(attr_pts_raw)})"
            )
        return msg

    @filter.llm_tool(name="rpg_add_exp")
    async def rpg_add_exp(self, event, target: str, exp_amount: int) -> str:
        """
        Add experience points to the character (e.g. from quests, exploration, or story events). Automatically handles level-ups.
        Args:
            exp_amount(int): Amount of EXP to add.
            target(string): Required. The character name to operate on (e.g. "Kirito").
        Returns:
            Updated status with any level-up notifications.
        """
        uid, char, err = self._require_char(event, target)
        if err:
            return err

        preset = self._world_preset(char)
        char["exp"] += exp_amount
        level_ups = apply_levelups(char, preset)
        self._persist(uid, char)

        if char.get("game_system") == "dnd5e":
            con_score = int(_lookup_char_attr(char, "CON") or 10)
            hp_gain = max(
                1,
                char.get("hit_die", 8) // 2 + 1 + dnd5e_ability_modifier(con_score),
            )
            lines = []
            for level in level_ups:
                line = (
                    f"🎊 升级!→ Lv.{level}\n   HP +{hp_gain} "
                    f"熟练加值 {dnd5e_proficiency_bonus(level):+d}"
                )
                if level in DND5E_ASI_LEVELS:
                    line += "\n   ⚡ 获得 2 点 ASI"
                lines.append(line)
        else:
            lines = [
                f"🎊 升级!→ Lv.{lv}\n   HP +{preset['hp_lv']} ATK +{preset['atk_lv']}"
                f" DEF +{preset['def_lv']} SPD +{preset['spd_lv']}"
                for lv in level_ups
            ]
        if not lines:
            lines.append(
                f"📊 +{exp_amount} EXP → {char['exp']}/{exp_needed(char['level'], preset)}"
            )
        lines.append("")
        lines.append(_format_status(char))
        return "\n".join(lines)

    @filter.llm_tool(name="rpg_equip_item")
    async def rpg_equip_item(
        self,
        event,
        target: str,
        item_name: str,
        slot: str,
        attributes: str = "",
        description: str = "",
        equip_condition: str = "",
        special_effect: str = "",
    ) -> str:
        """
        Equip an item to the character. Generates random stat bonuses based on the item name. Unequips the old item in the same slot first. Supports custom attributes, descriptions, and equip conditions.
        Args:
            item_name(string): The name of the item to equip (e.g. "铁剑", "阐释者", "皮甲").
            slot(string): Equipment slot. Common: "weapon"(武器), "armor"(护甲), "accessory"(饰品). Also supports custom slots like "main_weapon"(主武器), "sub_weapon"(副武器), "tactical_glasses"(战术眼镜), etc. Any string is accepted.
            attributes(string): Optional. Custom stat bonuses as JSON object. Keys: atk, def, hp, spd, plus custom attributes like STR/VIT/AGI/DEX/INT/LUK. Example: "{"atk":5,"def":3,"hp":20,"spd":-1}". Empty string = auto random based on item name.
            description(string): Optional. Custom item description/flavor text. Example: "传说中的圣剑,散发着金色光芒". Empty string = auto generated stat summary.
            equip_condition(string): Optional. Equip conditions the character must meet. Use comma-separated comparisons. Supports: level>=N, STR>=N, atk>N, def>=N, etc. Example: "level>=10,STR>=15". Empty string = no condition.
            special_effect(string): Optional. Special effect description for this equipment. Used by AI during battle/story narration. Example: "闪光弹: 致盲敌人2回合", "吸血: 攻击回复10%伤害". Empty string = no special effect.
            target(string): Required. The character name to operate on (e.g. "Kirito").
        Returns:
            Equipment result and updated character status.
        """
        uid, char, err = self._require_char(event, target)
        if err:
            return err
        slot = slot.lower()

        # 空名 = 卸下
        if item_name.strip() in ("", "无", "空", "卸下"):
            old = char.get("equipment", {}).get(slot)
            if not old:
                return "❌ 该槽位没有装备。"
            _take_item_bonuses(char, old)
            del char["equipment"][slot]
            self._persist(uid, char)
            return f"🔄 卸下{SLOT_CN.get(slot, slot)}: {old['name']}\n\n{_format_status(char)}"

        ok, reason = check_equip_conditions(char, equip_condition)
        if not ok:
            return f"❌ 装备条件不满足: {reason}"

        # 计算装备词条
        custom_attrs = parse_item_attributes(attributes)
        if custom_attrs:
            b_atk, b_def, b_hp, b_spd, _ = (
                int(custom_attrs.get("atk", 0)),
                int(custom_attrs.get("def", 0)),
                int(custom_attrs.get("hp", 0)),
                int(custom_attrs.get("spd", 0)),
                None,
            )
            custom_stat_changes = {
                k: v for k, v in custom_attrs.items() if k not in _BUILTIN_ITEM_ATTRS
            }
        else:
            b_atk, b_def, b_hp, b_spd, custom_stat_changes = _roll_random_bonuses(
                item_name
            )

        # 卸下旧装备的词条
        old = char.get("equipment", {}).get(slot)
        if old:
            _take_item_bonuses(char, old)

        # 构造装备对象
        item = {
            "name": item_name,
            "desc": "",
            "ba": b_atk,
            "bd": b_def,
            "bh": b_hp,
            "bs": b_spd,
        }
        if custom_stat_changes:
            item["custom"] = custom_stat_changes
        if equip_condition and equip_condition.strip():
            item["condition"] = equip_condition.strip()
        if special_effect and special_effect.strip():
            item["special_effect"] = special_effect.strip()

        stat_parts = []
        if b_atk:
            stat_parts.append(f"ATK+{b_atk}")
        if b_def:
            stat_parts.append(f"DEF+{b_def}")
        if b_hp:
            stat_parts.append(f"HP+{b_hp}")
        if b_spd:
            stat_parts.append(f"SPD{b_spd:+d}")
        for k, v in custom_stat_changes.items():
            stat_parts.append(f"{k}{v:+d}")
        stat_summary = " ".join(stat_parts) or "无属性变化"

        item["desc"] = (
            f"{description.strip()} [{stat_summary}]"
            if description and description.strip()
            else stat_summary
        )

        char.setdefault("equipment", {})[slot] = item
        _give_item_bonuses(char, b_atk, b_def, b_hp, b_spd, custom_stat_changes)
        self._persist(uid, char)

        cond_info = (
            f" (需: {equip_condition.strip()})"
            if equip_condition and equip_condition.strip()
            else ""
        )
        effect_info = (
            f"\n   ✦ 特殊效果: {special_effect.strip()}"
            if special_effect and special_effect.strip()
            else ""
        )
        return (
            f"⚔ 装备{SLOT_CN.get(slot, slot)}: {item_name}{cond_info}"
            f"\n   {item['desc']}{effect_info}\n\n{_format_status(char)}"
        )

    @filter.llm_tool(name="rpg_heal")
    async def rpg_heal(self, event, target: str, amount: int) -> str:
        """
        Heal the character's HP.
        Args:
            amount(int): Amount of HP to restore.
            target(string): Required. The character name to operate on (e.g. "Kirito").
        Returns:
            Healing result and current HP.
        """
        uid, char, err = self._require_char(event, target)
        if err:
            return err
        old_hp = char["hp"]
        char["hp"] = min(char["hp"] + amount, char["max_hp"])
        self._persist(uid, char)
        healed = char["hp"] - old_hp
        return f"💚 恢复 {healed} HP → {char['hp']}/{char['max_hp']}"

    @filter.llm_tool(name="rpg_take_damage")
    async def rpg_take_damage(
        self,
        event,
        target: str,
        raw_damage: int,
        source: str,
    ) -> str:
        """
        Apply damage to the character. Generic RPG sessions apply DEF reduction; DND 5E applies the rolled damage directly because AC only determines whether an attack hits.
        Args:
            raw_damage(int): Rolled damage. In DND 5E pass the final damage after dice and modifiers.
            source(string): Description of the damage source (e.g. "毒", "陷阱", "坠落").
            target(string): Required. The character name to operate on (e.g. "Kirito").
        Returns:
            Damage result with DEF reduction info and current HP.
        """
        uid, char, err = self._require_char(event, target)
        if err:
            return err
        if char.get("game_system") == "dnd5e":
            actual_damage = max(0, raw_damage)
            char["hp"] = max(0, char["hp"] - actual_damage)
            result = (
                f"💥 「{source}」伤害: {actual_damage}(DND 5E 的 AC 仅判定命中,不减伤)\n"
                f"   HP: {char['hp']}/{char['max_hp']}"
            )
        else:
            actual_damage = max(1, raw_damage - char["def"] // 3)
            char["hp"] = max(0, char["hp"] - actual_damage)
            result = (
                f"💥 「{source}」伤害: {raw_damage} → 实际 {actual_damage}"
                f"(DEF减免 {raw_damage - actual_damage})\n"
                f"   HP: {char['hp']}/{char['max_hp']}"
            )
        self._persist(uid, char)
        if char["hp"] <= 0:
            result += "\n💀 你倒下了……"
        return result

    @filter.llm_tool(name="rpg_add_effect")
    async def rpg_add_effect(
        self,
        event,
        target: str,
        effect_name: str,
        is_debuff: bool,
    ) -> str:
        """
        Add a buff or debuff status effect to the character.
        Args:
            effect_name(string): Name of the effect (e.g. "攻击力UP", "中毒", "加速", "诅咒").
            is_debuff(bool): True if this is a negative/debuff effect, False if positive/buff.
            target(string): Required. The character name to operate on (e.g. "Kirito").
        Returns:
            Confirmation message.
        """
        uid, char, err = self._require_char(event, target)
        if err:
            return err
        key = "debuffs" if is_debuff else "buffs"
        char.setdefault(key, []).append(effect_name)
        self._persist(uid, char)
        icon = "🔻" if is_debuff else "🔺"
        label = "减益" if is_debuff else "增益"
        return f"{icon} 获得{label}: {effect_name}"

    @filter.llm_tool(name="rpg_manage_currency")
    async def rpg_manage_currency(
        self,
        event,
        target: str,
        amount: int,
        action: str,
    ) -> str:
        """
        Manage the character's currency (gold, coins, etc). Used for buying items, quest rewards, etc.
        Args:
            target(string): Required. The character name to operate on (e.g. "Kirito").
            amount(int): Amount of currency. Always positive.
            action(string): "add" to earn currency, "spend" to spend currency, "set" to set to exact amount.
        Returns:
            Currency transaction result and current balance.
        """
        uid, char, err = self._require_char(event, target)
        if err:
            return err
        old = char.get("currency", 0)
        if action == "add":
            char["currency"] = old + amount
        elif action == "spend":
            if old < amount:
                return f"💸 余额不足!当前: {old},需要: {amount}"
            char["currency"] = old - amount
        elif action == "set":
            char["currency"] = amount
        else:
            return "❌ action 必须是 add / spend / set"
        self._persist(uid, char)
        return f"💰 {action} {amount} → 余额: {char['currency']}"

    @filter.llm_tool(name="rpg_set_attribute")
    async def rpg_set_attribute(
        self,
        event,
        target: str,
        attribute: str,
        value: int,
        mode: str,
    ) -> str:
        """
        Directly modify a character attribute. DND 5E ability scores are protected and must use rpg_allocate_point with earned ASI points.
        Args:
            attribute(string): Attribute name. Built-in: "hp", "max_hp", "atk", "def", "spd", "exp", "currency", "kills". For custom attributes (e.g. "mana", "sanity", "reputation"), they are auto-created.
            value(int): The value to apply.
            mode(string): "add" to add/subtract from current, "set" to set to exact value, "max" to cap at this value (cannot exceed).
            target(string): Required. The character name to operate on (e.g. "Kirito").
        Returns:
            Attribute change result.
        """
        uid, char, err = self._require_char(event, target)
        if err:
            return err
        normalized_attribute = attribute.strip().upper()
        if normalized_attribute.startswith("BASE_"):
            normalized_attribute = normalized_attribute[5:]
        elif normalized_attribute.startswith("ALLOC_"):
            normalized_attribute = normalized_attribute[6:]
        if (
            char.get("game_system") == "dnd5e"
            and normalized_attribute in DND5E_ABILITIES
        ):
            return "❌ DND 5E 六维属性不能直接修改,请使用 rpg_allocate_point 分配已获得的 ASI。"
        old_val = char.get(attribute, 0)
        if mode == "add":
            new_val = old_val + value
        elif mode == "set":
            new_val = value
        elif mode == "max":
            new_val = min(old_val, value)
        else:
            return "❌ mode 必须是 add / set / max"
        char[attribute] = new_val
        if attribute == "hp" and "max_hp" in char:
            char["hp"] = min(char["hp"], char["max_hp"])
        self._persist(uid, char)
        return f"📊 {attribute}: {old_val} → {new_val}"

    @filter.llm_tool(name="rpg_allocate_point")
    async def rpg_allocate_point(
        self,
        event,
        target: str,
        attribute: str,
        points: int = 1,
    ) -> str:
        """
        Spend unspent attribute points on a specific stat. Works with any stat the world defines (GGO's STR/VIT/AGI/DEX/INT/LUK, or another world's 体力/魔力, etc). Points are stored separately from base stats.
        Args:
            attribute(string): Target attribute to allocate points into (must match a stat name defined by the world preset).
            points(int): How many points to spend (default 1 if invalid).
            target(string): Required. The character name to operate on (e.g. "Kirito").
        Returns:
            Result showing old value, new value, and remaining unspent points.
        """
        uid, char, err = self._require_char(event, target)
        if err:
            return err
        points = max(1, int(points))
        preset = self._world_preset(char)
        stats = [str(stat) for stat in preset.get("stats", [])]
        canonical_attribute = next(
            (stat for stat in stats if stat.lower() == attribute.strip().lower()),
            None,
        )
        if canonical_attribute is None:
            available_stats = ", ".join(stats) or "无"
            return f"❌ 未定义属性「{attribute}」。可分配属性: {available_stats}"
        attribute = canonical_attribute
        available = char.get("unspent_points", 0)
        if available <= 0:
            return "❌ 没有可分配的属性点。"
        if points > available:
            return f"❌ 属性点不足。当前剩余 {available} 点,你要求分配 {points} 点。"

        alloc_key, base_key = f"alloc_{attribute}", f"base_{attribute}"
        base_val = char.get(base_key, char.get(attribute, 5))
        old_total = base_val + char.get(alloc_key, 0)
        if char.get("game_system") == "dnd5e" and old_total + points > 20:
            return f"❌ DND 5E 属性上限为 20。{attribute} 当前 {old_total},最多还能加 {20 - old_total} 点。"
        char[alloc_key] = char.get(alloc_key, 0) + points
        char["unspent_points"] = available - points
        total = base_val + char[alloc_key]
        if char.get("game_system") == "dnd5e":
            old_modifier = dnd5e_ability_modifier(old_total)
            new_modifier = dnd5e_ability_modifier(total)
            if attribute == "CON" and new_modifier != old_modifier:
                hp_delta = char.get("level", 1) * (new_modifier - old_modifier)
                char["max_hp"] = max(1, char["max_hp"] + hp_delta)
                char["hp"] = max(0, min(char["hp"] + hp_delta, char["max_hp"]))
            if attribute == "DEX" and new_modifier != old_modifier:
                char["def"] += new_modifier - old_modifier
            if attribute in ("STR", "DEX"):
                str_score = int(_lookup_char_attr(char, "STR") or 10)
                dex_score = int(_lookup_char_attr(char, "DEX") or 10)
                char["atk"] = dnd5e_proficiency_bonus(char.get("level", 1)) + max(
                    dnd5e_ability_modifier(str_score),
                    dnd5e_ability_modifier(dex_score),
                )
        self._persist(uid, char)
        msg = (
            f"📊 {attribute}: {total - points} → {total}"
            f"(+{points}点,基础{base_val}+{char[alloc_key]})"
        )
        remaining = char["unspent_points"]
        return msg + (
            f"\n⚡ 剩余属性点: {remaining}"
            if remaining > 0
            else "\n✅ 属性点已全部分配完毕!"
        )

    @filter.llm_tool(name="rpg_manage_inventory")
    async def rpg_manage_inventory(
        self,
        event,
        target: str,
        action: str,
        item_name: str = "",
    ) -> str:
        """
        Add or remove items from the character's inventory.
        Args:
            target(string): Required. The character name to operate on (e.g. "Kirito").
            action(string): "add" to add item, "remove" to remove item, "list" to list all items.
            item_name(string): Name of the item. Required for add/remove, optional for list.
        Returns:
            Inventory result.
        """
        uid, char, err = self._require_char(event, target)
        if err:
            return err
        inv = char.setdefault("inventory", [])
        if action == "list":
            if not inv:
                return "🎒 背包是空的。"
            counts = {}
            for it in inv:
                counts[it] = counts.get(it, 0) + 1
            lines = ["🎒 背包:"]
            for it, cnt in counts.items():
                lines.append(f"  · {it}" + (f" ×{cnt}" if cnt > 1 else ""))
            return "\n".join(lines)
        if action == "add":
            inv.append(item_name)
            self._persist(uid, char)
            return f"🎒 获得: {item_name}"
        if action == "remove":
            if item_name in inv:
                inv.remove(item_name)
                self._persist(uid, char)
                return f"🎒 移除: {item_name}"
            return f"🎒 背包里没有「{item_name}」"
        return "❌ action 必须是 add / remove / list"

    @filter.llm_tool(name="rpg_reset_character")
    async def rpg_reset_character(self, event, target: str) -> str:
        """
        Delete the current character data and start fresh. Use when starting a new life simulation.
        Args:
            target(string): Required. The character name to operate on (e.g. "Kirito").
        Returns:
            Confirmation that the character has been reset.
        """
        uid = self._resolve_uid(event, target)
        path = _char_path(self.data_dir, uid)
        if os.path.exists(path):
            os.remove(path)
            return "🗑 角色数据已清除,可以重新开始了。"
        return "ℹ️ 没有找到角色数据。"

    @filter.llm_tool(name="rpg_cleanup_old_data")
    async def rpg_cleanup_old_data(self, event, inactive_days: int) -> str:
        """
        Clean up old character save files AND game sessions that haven't been modified in the specified number of days. Deleting a session also removes its member character saves.
        Args:
            inactive_days(int): Delete saves/sessions not modified in this many days. Minimum 7.
        Returns:
            Summary of deleted files and sessions.
        """
        inactive_days = max(7, inactive_days)
        cutoff = time.time() - inactive_days * 86400

        # 过期会话 → 连带删除成员角色
        sess_dir = os.path.join(self.data_dir, "sessions")
        deleted_sessions: list[str] = []
        if os.path.exists(sess_dir):
            for fname in os.listdir(sess_dir):
                if not fname.endswith(".json"):
                    continue
                fpath = os.path.join(sess_dir, fname)
                if os.path.getmtime(fpath) >= cutoff:
                    continue
                session_id = fname[:-5]
                session = load_session(self.data_dir, session_id)
                if session:
                    group_id = self._get_group_id(event)
                    for name in session.get("members", []):
                        save = _char_path(
                            self.data_dir,
                            self._make_char_uid(group_id, name, self._uid(event)),
                        )
                        if os.path.exists(save):
                            os.remove(save)
                os.remove(fpath)
                deleted_sessions.append(session_id)

        # 过期存档
        save_dir = os.path.join(self.data_dir, "rpg_saves")
        deleted_saves: list[str] = []
        kept = 0
        if os.path.exists(save_dir):
            for fname in os.listdir(save_dir):
                if not fname.endswith(".json"):
                    continue
                fpath = os.path.join(save_dir, fname)
                if os.path.getmtime(fpath) < cutoff:
                    os.remove(fpath)
                    deleted_saves.append(fname[:-5])
                else:
                    kept += 1

        results = []
        if deleted_sessions:
            results.append(f"🗑 过期会话: {', '.join(deleted_sessions)}")
        if deleted_saves:
            results.append(f"🗑 过期存档: {len(deleted_saves)} 个")
        if not results:
            return (
                f"ℹ️ 没有超过 {inactive_days} 天未活跃的数据。当前保留 {kept} 个存档。"
            )
        results.append(f"📦 剩余活跃存档: {kept} 个")
        return f"✅ 清理完成({inactive_days}天未活跃):\n" + "\n".join(results)
