"""对战常用道具表(手工整理,中文名 + 说明 + 引擎效果参数)。

`effect` 的字段由 `engine.py` 解释;未实现的字段只作为说明展示给 LLM,
不会影响结算。数据量刻意控制在对战常用范围内。
"""

from __future__ import annotations

from .dex import _norm

# effect 支持的键(engine 读取):
#   stat_mult: {stat: 倍率}              常驻数值倍率(如讲究系列 / 突击背心)
#   type_mult: {Type: 倍率}              指定属性招式威力倍率(如木炭)
#   category_mult: {Physical/Special: 倍率}
#   damage_mult: 倍率                    全招式威力倍率(生命宝珠)
#   super_effective_mult: 倍率           效果拔群时额外倍率(达人带)
#   end_turn_heal / end_turn_damage: 分数
#   on_low_hp: {threshold, heal}         低血量自动回复树果
#   cure_status: bool                    异常状态自动回复(木子果)
#   self_status: brn/tox                 回合结束自我施加异常(火珠/毒珠)
#   contact_recoil: 分数                 被接触打中反伤(凸凸头盔)
#   focus_sash: bool                     满血被秒时留 1 HP
#   no_hazards: bool                     无视入场钉子
#   ground_immune: bool                  免疫地面
#   no_secondary: bool                   无视对手附加效果
#   no_stat_drop: bool                   不会被降能力
#   no_weather_damage: bool              不吃天气伤害
#   screen_turns / weather_turns / terrain_turns: 8
#   weather: 对应天气
#   multi_hit_min: 连续技最低次数
#   choice_lock: bool
#   no_status_moves: bool
#   requires_nfe: bool                   仅未进化完全时生效(进化奇石)

_ITEMS: dict[str, dict] = {
    "leftovers": {"zh": "吃剩的东西", "desc": "每回合结束回复最大 HP 的 1/16。", "effect": {"end_turn_heal": 1 / 16}},
    "black-sludge": {"zh": "黑色污泥", "desc": "毒属性每回合回复 1/16;其他属性每回合损失 1/8。", "effect": {"end_turn_heal_poison": 1 / 16, "end_turn_damage_nonpoison": 1 / 8}},
    "sitrus-berry": {"zh": "文柚果", "desc": "HP 低于一半时回复最大 HP 的 1/4。", "effect": {"on_low_hp": {"threshold": 0.5, "heal": 0.25}}},
    "lum-berry": {"zh": "木子果", "desc": "陷入异常状态时自动治愈。", "effect": {"cure_status": True}},
    "life-orb": {"zh": "生命宝珠", "desc": "招式威力 ×1.3,攻击后损失最大 HP 的 1/10。", "effect": {"damage_mult": 1.3, "recoil": 0.1}},
    "choice-band": {"zh": "讲究头带", "desc": "攻击 ×1.5,但只能使用同一招式。", "effect": {"stat_mult": {"atk": 1.5}, "choice_lock": True}},
    "choice-specs": {"zh": "讲究眼镜", "desc": "特攻 ×1.5,但只能使用同一招式。", "effect": {"stat_mult": {"spa": 1.5}, "choice_lock": True}},
    "choice-scarf": {"zh": "讲究围巾", "desc": "速度 ×1.5,但只能使用同一招式。", "effect": {"stat_mult": {"spe": 1.5}, "choice_lock": True}},
    "assault-vest": {"zh": "突击背心", "desc": "特防 ×1.5,但无法使用变化招式。", "effect": {"stat_mult": {"spd": 1.5}, "no_status_moves": True}},
    "eviolite": {"zh": "进化奇石", "desc": "未进化完全时防御与特防 ×1.5。", "effect": {"stat_mult": {"def": 1.5, "spd": 1.5}, "requires_nfe": True}},
    "focus-sash": {"zh": "气势披带", "desc": "满 HP 被一击击倒时留下 1 HP(每场一次)。", "effect": {"focus_sash": True}},
    "rocky-helmet": {"zh": "凸凸头盔", "desc": "被接触类招式命中时,反弹攻击者最大 HP 的 1/6。", "effect": {"contact_recoil": 1 / 6}},
    "heavy-duty-boots": {"zh": "厚底靴", "desc": "不会受到入场陷阱的伤害与效果。", "effect": {"no_hazards": True}},
    "light-clay": {"zh": "光之黏土", "desc": "光墙 / 反射壁持续时间延长至 8 回合。", "effect": {"screen_turns": 8}},
    "heat-rock": {"zh": "炽热岩石", "desc": "大晴天持续时间延长至 8 回合。", "effect": {"weather_turns": 8, "weather": "sun"}},
    "damp-rock": {"zh": "潮湿岩石", "desc": "求雨持续时间延长至 8 回合。", "effect": {"weather_turns": 8, "weather": "rain"}},
    "smooth-rock": {"zh": "平滑岩石", "desc": "沙暴持续时间延长至 8 回合。", "effect": {"weather_turns": 8, "weather": "sand"}},
    "icy-rock": {"zh": "冰冷岩石", "desc": "下雪持续时间延长至 8 回合。", "effect": {"weather_turns": 8, "weather": "snow"}},
    "terrain-extender": {"zh": "大地膜", "desc": "场地持续时间延长至 8 回合。", "effect": {"terrain_turns": 8}},
    "booster-energy": {"zh": "驱劲能量", "desc": "悖谬宝可梦的古代/未来特性本场立即发动。", "effect": {"paradox_boost": True}},
    "loaded-dice": {"zh": "作弊骰子", "desc": "连续招式至少命中 4 次。", "effect": {"multi_hit_min": 4}},
    "covert-cloak": {"zh": "密探斗篷", "desc": "不会受到对手招式的追加效果影响。", "effect": {"no_secondary": True}},
    "clear-amulet": {"zh": "清净坠饰", "desc": "不会被对手降低能力。", "effect": {"no_stat_drop": True}},
    "weakness-policy": {"zh": "弱点保险", "desc": "被效果拔群招式命中时,攻击与特攻 +2。", "effect": {"on_super_effective": {"boosts": {"atk": 2, "spa": 2}}}},
    "throat-spray": {"zh": "爽喉喷雾", "desc": "使用声音招式后特攻 +1。", "effect": {"on_sound_move": {"boosts": {"spa": 1}}}},
    "air-balloon": {"zh": "气球", "desc": "漂浮,免疫地面招式;被击中后爆掉。", "effect": {"ground_immune": True}},
    "safety-goggles": {"zh": "防尘护目镜", "desc": "不受天气伤害与粉末招式影响。", "effect": {"no_weather_damage": True}},
    "expert-belt": {"zh": "达人带", "desc": "对效果拔群的目标招式威力 ×1.2。", "effect": {"super_effective_mult": 1.2}},
    "muscle-band": {"zh": "力量头带", "desc": "物理招式威力 ×1.1。", "effect": {"category_mult": {"Physical": 1.1}}},
    "wise-glasses": {"zh": "博识眼镜", "desc": "特殊招式威力 ×1.1。", "effect": {"category_mult": {"Special": 1.1}}},
    "toxic-orb": {"zh": "剧毒宝珠", "desc": "回合结束时自身陷入剧毒。", "effect": {"self_status": "tox"}},
    "flame-orb": {"zh": "火焰宝珠", "desc": "回合结束时自身陷入灼伤。", "effect": {"self_status": "brn"}},
    "silk-scarf": {"zh": "丝绸围巾", "desc": "一般属性招式威力 ×1.2。", "effect": {"type_mult": {"Normal": 1.2}}},
}

# 属性增强道具(1.2×)
_TYPE_ITEMS = {
    "charcoal": ("木炭", "Fire"),
    "mystic-water": ("神秘水滴", "Water"),
    "miracle-seed": ("奇迹种子", "Grass"),
    "magnet": ("磁铁", "Electric"),
    "never-melt-ice": ("不融冰", "Ice"),
    "black-belt": ("黑带", "Fighting"),
    "poison-barb": ("毒针", "Poison"),
    "soft-sand": ("软沙", "Ground"),
    "sharp-beak": ("锐利鸟嘴", "Flying"),
    "twisted-spoon": ("弯曲的汤匙", "Psychic"),
    "silver-powder": ("银粉", "Bug"),
    "hard-stone": ("硬石头", "Rock"),
    "spell-tag": ("诅咒之符", "Ghost"),
    "dragon-fang": ("龙之牙", "Dragon"),
    "black-glasses": ("黑色眼镜", "Dark"),
    "metal-coat": ("金属膜", "Steel"),
    "fairy-feather": ("妖精之羽", "Fairy"),
}
for _k, (_zh, _t) in _TYPE_ITEMS.items():
    _ITEMS[_k] = {
        "zh": _zh,
        "desc": f"{_t} 属性招式威力 ×1.2。",
        "effect": {"type_mult": {_t: 1.2}},
    }


ITEMS: dict[str, dict] = _ITEMS
_ITEM_IDX: dict[str, str] = {}
for _key, _v in ITEMS.items():
    for _alias in (_key, _v["zh"]):
        _ITEM_IDX.setdefault(_norm(_alias), _key)


def resolve_item(query: str) -> tuple[str, dict] | None:
    """把道具名(中/英/标识)解析成 (key, entry)。"""
    if not query:
        return None
    raw = str(query).strip()
    if not raw:
        return None
    if raw in ITEMS:
        return raw, ITEMS[raw]
    key = _ITEM_IDX.get(_norm(raw))
    if key:
        return key, ITEMS[key]
    return None


def item_label(key: str | None) -> str:
    if not key:
        return ""
    entry = ITEMS.get(key)
    return entry["zh"] if entry else key


# ════════════════════════════════════════════════════════════════
# 背包道具(可消耗):精灵球 / 伤药 / 状态回复 / 复活 / PP 回复 /
# 战斗强化 / 进化石 / 稀有用具。effect 由 engine 与 tools 解释。
# ════════════════════════════════════════════════════════════════

# effect 支持的键:
#   ball_bonus: 固定捕获加成; ball_master: 必中;
#   ball_net / ball_nest / ball_timer / ball_quick / ball_dusk /
#   ball_repeat / ball_beast / ball_heavy / ball_level: 条件加成(engine 计算)
#   heal_hp / heal_hp_frac / heal_full: 回复 HP
#   cure_status: True(全部) 或 ["brn",...]; revive / revive_full: 复活
#   pp_restore / pp_restore_all: PP 回复
#   stat_boost: {stat: stages} 仅战斗中使用

_BAG: dict[str, dict] = {
    # ── 精灵球 ──
    "poke-ball": {"zh": "精灵球", "kind": "ball", "desc": "捕获宝可梦的基本球。", "effect": {"ball_bonus": 1.0}},
    "great-ball": {"zh": "超级球", "kind": "ball", "desc": "比精灵球更容易捕获。", "effect": {"ball_bonus": 1.5}},
    "ultra-ball": {"zh": "高级球", "kind": "ball", "desc": "捕获性能很高的球。", "effect": {"ball_bonus": 2.0}},
    "master-ball": {"zh": "大师球", "kind": "ball", "desc": "必定捕获任何宝可梦。", "effect": {"ball_master": True}},
    "premier-ball": {"zh": "纪念球", "kind": "ball", "desc": "纪念用的稀有球,性能同精灵球。", "effect": {"ball_bonus": 1.0}},
    "heal-ball": {"zh": "治愈球", "kind": "ball", "desc": "捕获后回复宝可梦的 HP 与异常状态。", "effect": {"ball_bonus": 1.0, "ball_heal": True}},
    "net-ball": {"zh": "捕网球", "kind": "ball", "desc": "对水/虫属性宝可梦效果极佳。", "effect": {"ball_bonus": 1.0, "ball_net": 3.5}},
    "dusk-ball": {"zh": "黑暗球", "kind": "ball", "desc": "在洞窟/夜晚更容易捕获。", "effect": {"ball_bonus": 1.0, "ball_dusk": 3.0}},
    "quick-ball": {"zh": "先机球", "kind": "ball", "desc": "战斗刚开始时极易捕获。", "effect": {"ball_bonus": 1.0, "ball_quick": 5.0}},
    "timer-ball": {"zh": "计时球", "kind": "ball", "desc": "回合越多越容易捕获。", "effect": {"ball_bonus": 1.0, "ball_timer": True}},
    "repeat-ball": {"zh": "重复球", "kind": "ball", "desc": "对已捕获过的种类更容易捕获。", "effect": {"ball_bonus": 1.0, "ball_repeat": 3.5}},
    "nest-ball": {"zh": "巢穴球", "kind": "ball", "desc": "对手等级越低越容易捕获。", "effect": {"ball_bonus": 1.0, "ball_nest": True}},
    "level-ball": {"zh": "等级球", "kind": "ball", "desc": "对手等级越低越容易捕获。", "effect": {"ball_bonus": 1.0, "ball_level": True}},
    "heavy-ball": {"zh": "沉重球", "kind": "ball", "desc": "对体重大的宝可梦更容易捕获。", "effect": {"ball_bonus": 1.0, "ball_heavy": True}},
    "beast-ball": {"zh": "究极球", "kind": "ball", "desc": "对究极异兽效果极佳。", "effect": {"ball_bonus": 1.0, "ball_beast": 5.0}},
    # ── 伤药 / 回复 ──
    "potion": {"zh": "伤药", "kind": "medicine", "desc": "回复 20 HP。", "effect": {"heal_hp": 20}},
    "super-potion": {"zh": "好伤药", "kind": "medicine", "desc": "回复 60 HP。", "effect": {"heal_hp": 60}},
    "hyper-potion": {"zh": "厉害伤药", "kind": "medicine", "desc": "回复 120 HP。", "effect": {"heal_hp": 120}},
    "max-potion": {"zh": "全满药", "kind": "medicine", "desc": "完全回复 HP。", "effect": {"heal_full": True}},
    "full-restore": {"zh": "全复药", "kind": "medicine", "desc": "完全回复 HP 并治愈异常状态。", "effect": {"heal_full": True, "cure_status": True}},
    "fresh-water": {"zh": "美味之水", "kind": "medicine", "desc": "回复 30 HP。", "effect": {"heal_hp": 30}},
    "soda-pop": {"zh": "汽水", "kind": "medicine", "desc": "回复 50 HP。", "effect": {"heal_hp": 50}},
    "lemonade": {"zh": "柠檬汁", "kind": "medicine", "desc": "回复 80 HP。", "effect": {"heal_hp": 80}},
    "moomoo-milk": {"zh": "哞哞鲜奶", "kind": "medicine", "desc": "回复 100 HP。", "effect": {"heal_hp": 100}},
    # ── 状态回复 ──
    "antidote": {"zh": "解毒药", "kind": "status", "desc": "治愈中毒。", "effect": {"cure_status": ["psn", "tox"]}},
    "burn-heal": {"zh": "灼伤药", "kind": "status", "desc": "治愈灼伤。", "effect": {"cure_status": ["brn"]}},
    "ice-heal": {"zh": "解冻药", "kind": "status", "desc": "治愈冰冻。", "effect": {"cure_status": ["frz"]}},
    "awakening": {"zh": "解眠药", "kind": "status", "desc": "唤醒睡眠。", "effect": {"cure_status": ["slp"]}},
    "paralyze-heal": {"zh": "解麻药", "kind": "status", "desc": "治愈麻痹。", "effect": {"cure_status": ["par"]}},
    "full-heal": {"zh": "万灵药", "kind": "status", "desc": "治愈任何异常状态。", "effect": {"cure_status": True}},
    # ── 复活 ──
    "revive": {"zh": "活力碎片", "kind": "revive", "desc": "复活并回复一半 HP。", "effect": {"revive": 0.5}},
    "max-revive": {"zh": "活力块", "kind": "revive", "desc": "复活并完全回复 HP。", "effect": {"revive_full": True}},
    # ── PP 回复 ──
    "ether": {"zh": "元气之粉", "kind": "pp", "desc": "回复一个招式 10 点 PP。", "effect": {"pp_restore": 10}},
    "max-ether": {"zh": "特攻之粉", "kind": "pp", "desc": "完全回复一个招式的 PP。", "effect": {"pp_restore_all": 1}},
    "elixir": {"zh": "秘药", "kind": "pp", "desc": "回复全部招式各 10 点 PP。", "effect": {"pp_restore": 10, "pp_all": True}},
    "max-elixir": {"zh": "厉害秘药", "kind": "pp", "desc": "完全回复全部招式的 PP。", "effect": {"pp_restore_all": 1, "pp_all": True}},
    # ── 战斗强化道具(仅战斗中使用) ──
    "x-attack": {"zh": "力量强化", "kind": "battle", "desc": "战斗中提升攻击。", "effect": {"stat_boost": {"atk": 1}}},
    "x-defense": {"zh": "防御强化", "kind": "battle", "desc": "战斗中提升防御。", "effect": {"stat_boost": {"def": 1}}},
    "x-special": {"zh": "特攻强化", "kind": "battle", "desc": "战斗中提升特攻。", "effect": {"stat_boost": {"spa": 1}}},
    "x-sp-defense": {"zh": "特防强化", "kind": "battle", "desc": "战斗中提升特防。", "effect": {"stat_boost": {"spd": 1}}},
    "x-speed": {"zh": "速度强化", "kind": "battle", "desc": "战斗中提升速度。", "effect": {"stat_boost": {"spe": 1}}},
    "dire-hit": {"zh": "要害强化", "kind": "battle", "desc": "战斗中提升会心一击率。", "effect": {"focus_energy": True}},
    "guard-spec": {"zh": "要害防御", "kind": "battle", "desc": "防止对手要害一击。", "effect": {"guard_spec": True}},
    # ── 树果(可携带或使用) ──
    "oran-berry": {"zh": "橙橙果", "kind": "berry", "desc": "回复 10 HP。", "effect": {"heal_hp": 10}},
    "cheri-berry": {"zh": "蔓莓果", "kind": "berry", "desc": "治愈麻痹。", "effect": {"cure_status": ["par"]}},
    "chesto-berry": {"zh": "迷雾果", "kind": "berry", "desc": "唤醒睡眠。", "effect": {"cure_status": ["slp"]}},
    "pecha-berry": {"zh": "桃桃果", "kind": "berry", "desc": "治愈中毒。", "effect": {"cure_status": ["psn", "tox"]}},
    "rawst-berry": {"zh": "苦味果", "kind": "berry", "desc": "治愈灼伤。", "effect": {"cure_status": ["brn"]}},
    "aspear-berry": {"zh": "亚开果", "kind": "berry", "desc": "治愈冰冻。", "effect": {"cure_status": ["frz"]}},
    "lum-berry": {"zh": "木子果", "kind": "berry", "desc": "治愈任何异常状态。", "effect": {"cure_status": True}},
    "sitrus-berry": {"zh": "文柚果", "kind": "berry", "desc": "回复最大 HP 的 1/4。", "effect": {"heal_hp_frac": 0.25}},
    # ── 稀有用具 ──
    "rare-candy": {"zh": "神奇糖果", "kind": "rare", "desc": "提升 1 级。", "effect": {"level_up": 1}},
    "pp-up": {"zh": "PP 提升剂", "kind": "rare", "desc": "提升一个招式的 PP 上限。", "effect": {"pp_up": 1}},
    "pp-max": {"zh": "PP 极限提升剂", "kind": "rare", "desc": "将招式的 PP 上限提到最大。", "effect": {"pp_up": 999}},
    "ability-capsule": {"zh": "特性胶囊", "kind": "rare", "desc": "切换到另一个普通特性。", "effect": {"ability_switch": True}},
    "ability-patch": {"zh": "特性膏药", "kind": "rare", "desc": "切换到隐藏特性。", "effect": {"ability_patch": True}},
}

# 进化石
_STONES = {
    "fire-stone": ("火之石", "Fire"),
    "water-stone": ("水之石", "Water"),
    "thunder-stone": ("雷之石", "Electric"),
    "leaf-stone": ("叶之石", "Grass"),
    "moon-stone": ("月之石", "Moon"),
    "sun-stone": ("日之石", "Sun"),
    "shiny-stone": ("光之石", "Shiny"),
    "dusk-stone": ("暗之石", "Dusk"),
    "dawn-stone": ("觉醒之石", "Dawn"),
    "ice-stone": ("冰之石", "Ice"),
}
for _k, (_zh, _tag) in _STONES.items():
    _BAG[_k] = {
        "zh": _zh,
        "kind": "stone",
        "desc": "特定的宝可梦使用后会进化。",
        "effect": {"evolve_stone": _tag},
    }

BAG_ITEMS: dict[str, dict] = _BAG
_BAG_IDX: dict[str, str] = {}
for _key, _v in BAG_ITEMS.items():
    for _alias in (_key, _v["zh"]):
        _BAG_IDX.setdefault(_norm(_alias), _key)

KIND_ZH = {
    "ball": "精灵球",
    "medicine": "回复",
    "status": "状态回复",
    "revive": "复活",
    "pp": "PP 回复",
    "battle": "战斗强化",
    "berry": "树果",
    "rare": "稀有用具",
    "stone": "进化石",
}
KIND_ORDER = ["ball", "medicine", "status", "revive", "pp", "battle", "berry", "stone", "rare"]


def resolve_bag_item(query: str) -> tuple[str, dict] | None:
    """把背包道具名解析成 (key, entry)。"""
    if not query:
        return None
    raw = str(query).strip()
    if raw in BAG_ITEMS:
        return raw, BAG_ITEMS[raw]
    key = _BAG_IDX.get(_norm(raw))
    if key:
        return key, BAG_ITEMS[key]
    # 兼容持有道具表里的名字(背包里也可能存)
    r = resolve_item(raw)
    if r and r[0] in BAG_ITEMS:
        return r[0], BAG_ITEMS[r[0]]
    return None


def bag_item_label(key: str | None) -> str:
    if not key:
        return ""
    entry = BAG_ITEMS.get(key)
    return entry["zh"] if entry else key
