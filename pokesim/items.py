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
