"""单打对战引擎(基于第 9 世代机制,数据驱动)。

设计目标:给 LLM 提供**确定性、可序列化**的宝可梦对战结算,而不是让模型编数值。
覆盖范围:
- 属性相克(含双属性)、太晶化(含星晶)、STAB
- 实际数值(种族值 / 个体值 / 努力值 / 性格)、能力等级、烧伤/麻痹对数值的影响
- 伤害公式(第 5 世代以后)+ 天气 / 会心 / 随机数 / 道具 / 特性修正
- 异常状态(灼伤/麻痹/中毒/剧毒/睡眠/冰冻)、混乱、畏缩、寄生种子
- 入场陷阱(隐形岩/撒菱/毒菱/黏黏网)、墙(反射壁/光墙/极光幕)、场地与天气
- 常用特性(威吓/飘浮/魔法守护/多重鳞片/毛茸茸/适应力/技术高手/救火员等)
- 换人、倒下替换、胜负判定

明确不模拟的部分(遇到时日志会提示):替身、变化类复杂控制(挑衅/再来一次)、
一击必杀、双打、部分专属特性。这些交由 LLM 叙事处理。

所有随机性来自 `seed`:同一次行动、相同 seed 结果完全一致,方便 /undo 与复现。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import ClassVar

from .dex import STAT_ORDER, get_dex
from .items import BAG_ITEMS, ITEMS, item_label

# ──────────────────────────── 常量 ────────────────────────────

STATUS_ZH = {
    "brn": "灼伤",
    "par": "麻痹",
    "psn": "中毒",
    "tox": "剧毒",
    "slp": "睡眠",
    "frz": "冰冻",
}
WEATHER_ZH = {
    "sun": "大晴天",
    "rain": "下雨",
    "sand": "沙暴",
    "snow": "下雪",
}
TERRAIN_ZH = {
    "electricterrain": "电气场地",
    "grassyterrain": "青草场地",
    "mistyterrain": "薄雾场地",
    "psychicterrain": "精神场地",
}
HAZARD_ZH = {
    "stealthrock": "隐形岩",
    "spikes": "撒菱",
    "toxicspikes": "毒菱",
    "stickyweb": "黏黏网",
}
SCREEN_ZH = {
    "reflect": "反射壁",
    "lightscreen": "光墙",
    "auroraveil": "极光幕",
    "tailwind": "顺风",
}
TYPE_EN_TO_KEY = {
    "Normal": "normal",
    "Fire": "fire",
    "Water": "water",
    "Electric": "electric",
    "Grass": "grass",
    "Ice": "ice",
    "Fighting": "fighting",
    "Poison": "poison",
    "Ground": "ground",
    "Flying": "flying",
    "Psychic": "psychic",
    "Bug": "bug",
    "Rock": "rock",
    "Ghost": "ghost",
    "Dragon": "dragon",
    "Dark": "dark",
    "Steel": "steel",
    "Fairy": "fairy",
    "Stellar": "stellar",
}

_FIXED_CHOICE_LOCK = {"choice-band", "choice-specs", "choice-scarf"}


def _stage_mult(stage: int, base: int = 2) -> float:
    stage = max(-6, min(6, int(stage)))
    if stage >= 0:
        return (base + stage) / base
    return base / (base - stage)


def _acc_mult(stage: int) -> float:
    stage = max(-6, min(6, int(stage)))
    if stage >= 0:
        return (3 + stage) / 3
    return 3 / (3 - stage)


# ──────────────────────────── 个体 ────────────────────────────


@dataclass
class Pokemon:
    """一只宝可梦的完整状态(可序列化)。"""

    species: str
    level: int = 5
    exp: int = 0
    nickname: str = ""
    nature: str = "hardy"
    ability: str = ""
    item: str = ""
    moves: list[str] = field(default_factory=list)
    tera_type: str = ""
    gender: str = ""
    friendship: int = 70
    ivs: dict = field(default_factory=dict)
    evs: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    max_hp: int = 1
    cur_hp: int = 1
    status: str = ""
    status_turns: int = 0
    stages: dict = field(default_factory=dict)
    volatiles: dict = field(default_factory=dict)
    terastallized: bool = False
    fainted: bool = False
    pp: dict = field(default_factory=dict)
    used_focus_sash: bool = False
    last_move: str = ""
    choice_locked: str = ""
    turns_active: int = 0
    faint_logged: bool = False

    # ── 展示 ──
    @property
    def entry(self) -> dict:
        return get_dex().species.get(self.species, {})

    @property
    def display(self) -> str:
        if self.nickname:
            return f"{self.nickname}({self.entry.get('zh', self.species)})"
        return self.entry.get("zh") or self.entry.get("name") or self.species

    @property
    def types(self) -> list[str]:
        """当前有效属性(太晶化后通常变单一属性;星晶保持原属性)。"""
        base = list(self.entry.get("types") or [])
        if self.terastallized and self.tera_type and self.tera_type != "Stellar":
            return [self.tera_type]
        return base

    @property
    def original_types(self) -> list[str]:
        return list(self.entry.get("types") or [])

    @property
    def ability_name(self) -> str:
        a = get_dex().abilities.get(self.ability) or {}
        return a.get("zh") or a.get("name") or self.ability

    def has_ability(self, *names: str) -> bool:
        return self.ability in names

    def hp_frac(self) -> float:
        return self.cur_hp / self.max_hp if self.max_hp else 0.0

    def stage(self, stat: str) -> int:
        return int(self.stages.get(stat, 0))

    def boost(self, stat: str, delta: int, *, quiet: bool = False) -> str:
        """能力等级变化,返回日志(可为空)。"""
        if delta == 0:
            return ""
        if delta < 0:
            item_eff = (ITEMS.get(self.item) or {}).get("effect") or {}
            if item_eff.get("no_stat_drop"):
                label = get_dex().stat_label(stat)
                return f"{self.display} 的{label}不会下降({item_label(self.item)})!"
            if self.has_ability("clear-body", "white-smoke", "full-metal-body"):
                return f"{self.display} 的{get_dex().stat_label(stat)}不会下降({self.ability_name})!"
        old = self.stage(stat)
        new = max(-6, min(6, old + delta))
        self.stages[stat] = new
        if new == old:
            return ""
        label = get_dex().stat_label(stat)
        if delta > 0:
            return f"{self.display} 的{label}提升了!({new:+d})"
        return f"{self.display} 的{label}下降了!({new:+d})"

    def reset_boosts(self) -> None:
        self.stages = {}

    def to_dict(self) -> dict:
        return {
            "species": self.species,
            "level": self.level,
            "exp": self.exp,
            "nickname": self.nickname,
            "nature": self.nature,
            "ability": self.ability,
            "item": self.item,
            "moves": list(self.moves),
            "tera_type": self.tera_type,
            "gender": self.gender,
            "friendship": self.friendship,
            "ivs": dict(self.ivs),
            "evs": dict(self.evs),
            "stats": dict(self.stats),
            "max_hp": self.max_hp,
            "cur_hp": self.cur_hp,
            "status": self.status,
            "status_turns": self.status_turns,
            "stages": dict(self.stages),
            "volatiles": dict(self.volatiles),
            "terastallized": self.terastallized,
            "fainted": self.fainted,
            "pp": dict(self.pp),
            "used_focus_sash": self.used_focus_sash,
            "last_move": self.last_move,
            "choice_locked": self.choice_locked,
            "turns_active": self.turns_active,
            "faint_logged": self.faint_logged,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Pokemon:
        p = cls(species=d.get("species", ""))
        for k in (
            "level",
            "exp",
            "nickname",
            "nature",
            "ability",
            "item",
            "tera_type",
            "gender",
            "friendship",
            "max_hp",
            "cur_hp",
            "status",
            "status_turns",
            "terastallized",
            "fainted",
            "used_focus_sash",
            "last_move",
            "choice_locked",
            "turns_active",
            "faint_logged",
        ):
            if k in d:
                setattr(p, k, d[k])
        p.moves = list(d.get("moves") or [])
        p.ivs = dict(d.get("ivs") or {})
        p.evs = dict(d.get("evs") or {})
        p.stats = dict(d.get("stats") or {})
        p.stages = dict(d.get("stages") or {})
        p.volatiles = dict(d.get("volatiles") or {})
        p.pp = dict(d.get("pp") or {})
        return p

    # ── 数值(战斗中)──

    def battle_stat(self, stat: str, battle: Battle | None = None) -> int:
        """计入道具/特性/状态/能力等级的实战数值。"""
        base = float(self.stats.get(stat, 1))
        item_eff = (ITEMS.get(self.item) or {}).get("effect") or {}

        # 道具常驻数值倍率
        for s, m in (item_eff.get("stat_mult") or {}).items():
            if s != stat:
                continue
            if item_eff.get("requires_nfe") and not self.entry.get("evos"):
                continue
            base *= m

        # 特性数值倍率
        if stat == "atk":
            if self.has_ability("huge-power", "pure-power"):
                base *= 2
            if self.has_ability("guts") and self.status:
                base *= 1.5
            if self.has_ability("hustle"):
                base *= 1.5
            if self.has_ability("toxic-boost") and self.status in ("psn", "tox"):
                base *= 1.5
            if self.has_ability("orichalcum-pulse") and battle and battle.weather == "sun":
                base *= 4 / 3
        if stat == "spa":
            if self.has_ability("solar-power") and battle and battle.weather == "sun":
                base *= 1.5
            if self.has_ability("flare-boost") and self.status == "brn":
                base *= 1.5
            if self.has_ability("hadron-engine") and battle and battle.terrain == "electricterrain":
                base *= 4 / 3
        if stat == "spe":
            if self.has_ability("swift-swim") and battle and battle.weather == "rain":
                base *= 2
            if self.has_ability("chlorophyll") and battle and battle.weather == "sun":
                base *= 2
            if self.has_ability("sand-rush") and battle and battle.weather == "sand":
                base *= 2
            if self.has_ability("slush-rush") and battle and battle.weather == "snow":
                base *= 2
            if self.has_ability("surge-surfer") and battle and battle.terrain == "electricterrain":
                base *= 2

        # 悖谬特性 / 驱劲能量(简化:对应天气或场地时提攻或提速)
        if battle and _paradox_active(battle, self, stat):
            base *= 1.3

        # 能力等级
        base *= _stage_mult(self.stage(stat))

        # 异常状态(灼伤只影响物理招式伤害,不降攻击数值;混乱自伤在此单独结算)
        if stat == "spe" and self.status == "par":
            base *= 0.5

        # 顺风
        if stat == "spe" and battle and _has_tailwind(battle, self):
            base *= 2

        # 对手「灾祸」特性降低数值(攻/特攻 → 物防/特防 等,简化处理)
        if battle and stat in ("atk", "spa", "def", "spd"):
            base *= _ruin_modifier(battle, self, stat)

        return max(1, int(base))

    def heal(self, amount: int) -> int:
        before = self.cur_hp
        self.cur_hp = min(self.max_hp, self.cur_hp + max(0, int(amount)))
        return self.cur_hp - before

    def take_damage(self, amount: int) -> int:
        before = self.cur_hp
        self.cur_hp = max(0, self.cur_hp - max(0, int(amount)))
        if self.cur_hp == 0:
            self.fainted = True
        return before - self.cur_hp

    def full_heal(self) -> None:
        self.cur_hp = self.max_hp
        self.fainted = False
        self.faint_logged = False
        self.status = ""
        self.status_turns = 0
        self.stages = {}
        self.volatiles = {}
        self.choice_locked = ""


# ──────────────────────────── 一方 / 对战 ────────────────────────────


@dataclass
class Side:
    name: str
    party: list[Pokemon] = field(default_factory=list)
    active: int = 0
    hazards: dict = field(default_factory=dict)
    screens: dict = field(default_factory=dict)
    tera_used: bool = False

    @property
    def mon(self) -> Pokemon | None:
        if 0 <= self.active < len(self.party):
            return self.party[self.active]
        return None

    def healthy(self) -> list[int]:
        return [i for i, p in enumerate(self.party) if not p.fainted]

    def alive(self) -> bool:
        return bool(self.healthy())

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "party": [p.to_dict() for p in self.party],
            "active": self.active,
            "hazards": dict(self.hazards),
            "screens": dict(self.screens),
            "tera_used": self.tera_used,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Side:
        s = cls(name=d.get("name", ""))
        s.party = [Pokemon.from_dict(p) for p in (d.get("party") or [])]
        s.active = int(d.get("active", 0) or 0)
        s.hazards = dict(d.get("hazards") or {})
        s.screens = dict(d.get("screens") or {})
        s.tera_used = bool(d.get("tera_used", False))
        return s


@dataclass
class Battle:
    player: Side
    enemy: Side
    weather: str = ""
    weather_turns: int = 0
    terrain: str = ""
    terrain_turns: int = 0
    field_effects: dict = field(default_factory=dict)
    turn: int = 0
    seed: int = 0
    log: list[str] = field(default_factory=list)
    finished: bool = False
    winner: str = ""
    awaiting_switch: bool = False
    player_damaged: bool = False
    enemy_damaged: bool = False
    wild: bool = False
    bag: dict = field(default_factory=dict)
    captured: dict | None = None
    run_attempts: int = 0
    escaped: bool = False
    # 每回合重置的随机数调用计数(避免同 salt 的随机数完全相关)
    _rng_calls: int = 0

    # ── 序列化 ──
    def to_dict(self) -> dict:
        return {
            "player": self.player.to_dict(),
            "enemy": self.enemy.to_dict(),
            "weather": self.weather,
            "weather_turns": self.weather_turns,
            "terrain": self.terrain,
            "terrain_turns": self.terrain_turns,
            "field_effects": dict(self.field_effects),
            "turn": self.turn,
            "seed": self.seed,
            "log": list(self.log),
            "finished": self.finished,
            "winner": self.winner,
            "awaiting_switch": self.awaiting_switch,
            "wild": self.wild,
            "bag": dict(self.bag),
            "captured": self.captured,
            "run_attempts": self.run_attempts,
            "escaped": self.escaped,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Battle:
        b = cls(
            player=Side.from_dict(d.get("player") or {}),
            enemy=Side.from_dict(d.get("enemy") or {}),
        )
        b.weather = d.get("weather", "")
        b.weather_turns = int(d.get("weather_turns", 0) or 0)
        b.terrain = d.get("terrain", "")
        b.terrain_turns = int(d.get("terrain_turns", 0) or 0)
        b.field_effects = dict(d.get("field_effects") or {})
        b.turn = int(d.get("turn", 0) or 0)
        b.seed = int(d.get("seed", 0) or 0)
        b.log = list(d.get("log") or [])
        b.finished = bool(d.get("finished", False))
        b.winner = d.get("winner", "")
        b.awaiting_switch = bool(d.get("awaiting_switch", False))
        b.wild = bool(d.get("wild", False))
        b.bag = dict(d.get("bag") or {})
        b.captured = d.get("captured")
        b.run_attempts = int(d.get("run_attempts", 0) or 0)
        b.escaped = bool(d.get("escaped", False))
        return b

    # ── 展示 ──
    def bar(self, poke: Pokemon) -> str:
        pct = round(poke.hp_frac() * 100)
        return f"{poke.cur_hp}/{poke.max_hp} ({pct}%)"

    def status_line(self, poke: Pokemon) -> str:
        bits = []
        if poke.status:
            bits.append(STATUS_ZH.get(poke.status, poke.status))
        if poke.terastallized:
            bits.append(f"太晶{poke.tera_type}")
        if poke.status_turns and poke.status in ("slp", "tox"):
            pass
        return " ".join(bits)

    def summary(self) -> str:
        lines = [f"【第 {self.turn} 回合】" + ("(野生战)" if self.wild else "(训练家战)")]
        for label, side in (("我方", self.player), ("对方", self.enemy)):
            mon = side.mon
            if mon is None:
                lines.append(f"{label}: 无出场宝可梦")
                continue
            st = self.status_line(mon)
            lines.append(
                f"{label}: {mon.display} Lv{mon.level} "
                f"[{mon.cur_hp}/{mon.max_hp}] "
                f"属性 {'/'.join(mon.types)} "
                + (f"{st} " if st else "")
                + f"| 特性 {mon.ability_name}"
                + (f" | 道具 {item_label(mon.item)}" if mon.item else "")
            )
            boosts = {
                get_dex().stat_label(s): v
                for s, v in mon.stages.items()
                if v
            }
            if boosts:
                lines.append("  能力变化: " + ", ".join(f"{k}{v:+d}" for k, v in boosts.items()))
        if self.weather:
            lines.append(f"天气: {WEATHER_ZH.get(self.weather, self.weather)}")
        if self.terrain:
            lines.append(f"场地: {TERRAIN_ZH.get(self.terrain, self.terrain)}")
        for label, side in (("我方", self.player), ("对方", self.enemy)):
            hz = [HAZARD_ZH.get(k, k) for k in side.hazards]
            sc = [f"{SCREEN_ZH.get(k, k)}({v})" for k, v in side.screens.items()]
            if hz or sc:
                lines.append(
                    f"{label}场地: " + " ".join(hz + sc)
                )
        if self.finished:
            if self.escaped:
                lines.append("战斗结束: 成功逃走")
            elif self.captured:
                lines.append("战斗结束: 捕获成功")
            else:
                lines.append(
                    f"战斗结束: {'我方胜利' if self.winner == 'player' else '对方胜利'}"
                )
        elif self.awaiting_switch:
            lines.append("⚠️ 我方宝可梦倒下,需要换人(用 switch <序号>)")
        if not self.finished and self.player.mon is not None:
            dex = get_dex()
            mon = self.player.mon
            if mon.moves:
                mv = " ".join(
                    f"{i}.{(dex.moves.get(m) or {}).get('zh', m)}"
                    f"({mon.pp.get(m, 0)}/{(dex.moves.get(m) or {}).get('pp', 0)})"
                    for i, m in enumerate(mon.moves, 1)
                )
                lines.append(f"可用招式: {mv}")
            if len(self.player.party) > 1:
                party = []
                for i, pm in enumerate(self.player.party, 1):
                    tag = "✗" if pm.fainted else STATUS_ZH.get(pm.status, pm.status)
                    mark = "←" if i - 1 == self.player.active else ""
                    party.append(f"{i}.{pm.display}[{pm.cur_hp}/{pm.max_hp}]{tag}{mark}")
                lines.append("我方队伍: " + " ".join(party))
        return "\n".join(lines)

    # ── RNG ──
    def _rng(self, salt: int = 0) -> random.Random:
        # 同一回合内每次调用都不同:seed/turn/salt/调用序号 共同决定,
        # 避免双方命中/会心/追加判定拿到相同随机数(完全相关)。
        self._rng_calls += 1
        return random.Random(
            (self.seed or 0) * 1000003
            + self.turn * 9176
            + salt * 131
            + self._rng_calls
        )

    # ── 入口 ──
    def start(self) -> list[str]:
        self.log = []
        self._rng_calls = 0
        self._send_out(self.player, self.player.active, initial=True)
        self._send_out(self.enemy, self.enemy.active, initial=True)
        self._check_faints()
        return list(self.log)

    def step(self, player_action: dict, enemy_action: dict | str = "auto") -> list[str]:
        """推进一回合。action 形如:
        {"type": "move", "move": "thunderbolt", "tera": true}
        {"type": "switch", "index": 2}
        {"type": "forfeit"}
        """
        if self.finished:
            return ["战斗已经结束。"]
        self.log = []
        # 等换人时只接受换人命令;无效行动不消耗回合(也不推进随机数流)
        if self.awaiting_switch and player_action.get("type") != "switch":
            return ['⚠️ 必须先换人:{"type": "switch", "index": <序号>}']
        self.turn += 1
        self._rng_calls = 0
        self.player_damaged = False
        self.enemy_damaged = False

        # 回合开始:清理上回合的保护/畏缩
        for side in (self.player, self.enemy):
            mon = side.mon
            if mon:
                mon.volatiles.pop("protect", None)
                mon.volatiles.pop("flinch", None)
                mon.volatiles.pop("endure", None)

        if self.awaiting_switch:
            self._do_switch(self.player, int(player_action.get("index", 0)))
            self.awaiting_switch = False
            enemy_action = "auto"
            self._tick_end_of_turn()
            self._check_faints()
            return list(self.log)

        if enemy_action == "auto":
            enemy_action = self.ai_action()

        if player_action.get("type") == "forfeit":
            self.finished = True
            self.winner = "enemy"
            self.log.append("我方主动认输,战斗结束。")
            return list(self.log)

        # 交换优先
        if player_action.get("type") == "switch":
            self._do_switch(self.player, int(player_action.get("index", 0)))
            self._after_switch(player_action)
        if enemy_action.get("type") == "switch":
            self._do_switch(self.enemy, int(enemy_action.get("index", 0)))

        # 太晶化在出招前处理
        if player_action.get("type") == "move" and player_action.get("tera"):
            self._terastallize(self.player)

        # 计算行动顺序
        order = self._action_order(player_action, enemy_action)
        for who in order:
            if self.finished:
                break
            act = player_action if who == "player" else enemy_action
            side = self.player if who == "player" else self.enemy
            other = self.enemy if who == "player" else self.player
            atype = act.get("type")
            if atype == "switch":
                continue  # 换人已在上面处理
            if atype == "move":
                self._execute_move(side, other, act)
            elif atype == "item":
                self._execute_battle_item(side, other, act)
            elif atype == "catch":
                self._execute_catch(side, other, act)
            elif atype == "run":
                self._execute_run(side, other)
            self._check_faints()
            if self.finished:
                break

        self._tick_end_of_turn()
        self._check_faints()
        return list(self.log)

    # ── 行动顺序 ──
    _NON_MOVE_PRIORITY: ClassVar[dict[str, int]] = {
        "switch": 6,
        "item": 6,
        "catch": 6,
        "run": 6,
    }

    def _action_priority(self, action: dict, side: Side) -> int:
        atype = action.get("type")
        if atype in self._NON_MOVE_PRIORITY:
            return self._NON_MOVE_PRIORITY[atype]
        if atype != "move":
            return 0
        mv = get_dex().moves.get(action.get("move", ""), {})
        pri = int(mv.get("priority", 0) or 0)
        mon = side.mon
        if mon and mv.get("category") == "Status" and mon.has_ability("prankster"):
            pri += 1
        return pri

    def _action_order(self, pa: dict, ea: dict) -> list[str]:
        pm = self.player.mon
        em = self.enemy.mon
        if pm is None or em is None:
            return ["player", "enemy"]
        pp = self._action_priority(pa, self.player)
        ep = self._action_priority(ea, self.enemy)
        if pp != ep:
            return ["player", "enemy"] if pp > ep else ["enemy", "player"]
        ps = pm.battle_stat("spe", self)
        es = em.battle_stat("spe", self)
        if ps != es:
            return ["player", "enemy"] if ps > es else ["enemy", "player"]
        rng = self._rng(1)
        return ["player", "enemy"] if rng.random() < 0.5 else ["enemy", "player"]

    # ── 出场 / 换人 ──
    def _send_out(self, side: Side, index: int, initial: bool = False) -> None:
        side.active = index
        mon = side.mon
        if mon is None:
            return
        mon.turns_active = 0
        self.log.append(f"{'我方' if side is self.player else '对方'}派出了 {mon.display}!")
        self._apply_hazards(side, mon)
        self._on_switch_in(side, mon, initial)

    def _do_switch(self, side: Side, index: int) -> None:
        if index < 0 or index >= len(side.party):
            return
        if index == side.active:
            return
        if side.party[index].fainted:
            self.log.append(f"{side.party[index].display} 已倒下,无法上场。")
            return
        old = side.mon
        if old and not old.fainted:
            self._on_switch_out(side, old)
        tag = "我方" if side is self.player else "对方"
        if old and not old.fainted:
            self.log.append(f"{tag}收回了 {old.display}。")
        self._send_out(side, index)

    def _after_switch(self, action: dict) -> None:
        pass

    def _on_switch_in(self, side: Side, mon: Pokemon, initial: bool) -> None:
        if mon.fainted:
            return
        # 威吓
        if mon.has_ability("intimidate"):
            foe = (self.enemy if side is self.player else self.player).mon
            if foe and not foe.fainted and not self._ignores_ability(mon):
                msg = foe.boost("atk", -1)
                if msg:
                    self.log.append(f"{mon.display} 的威吓!")
                    self.log.append(msg)
        # 天气 / 场地特性
        setters = {
            "drought": ("weather", "sun"),
            "drizzle": ("weather", "rain"),
            "sand-stream": ("weather", "sand"),
            "snow-warning": ("weather", "snow"),
            "electric-surge": ("terrain", "electricterrain"),
            "grassy-surge": ("terrain", "grassyterrain"),
            "misty-surge": ("terrain", "mistyterrain"),
            "psychic-surge": ("terrain", "psychicterrain"),
            "orichalcum-pulse": ("weather", "sun"),
            "hadron-engine": ("terrain", "electricterrain"),
        }
        if mon.ability in setters:
            kind, val = setters[mon.ability]
            self._set_field(kind, val, mon)
        # 入场陷阱已经在 _apply_hazards 处理
        if mon.fainted:
            return
        # 复制/下载类特性(简化)
        if mon.has_ability("download"):
            foe = (self.enemy if side is self.player else self.player).mon
            if foe:
                stat = "atk" if foe.battle_stat("def", self) < foe.battle_stat("spd", self) else "spa"
                self.log.append(mon.boost(stat, 1) or f"{mon.display} 下载完成!")
        if mon.has_ability("intrepid-sword"):
            self.log.append(mon.boost("atk", 1))
        if mon.has_ability("dauntless-shield"):
            self.log.append(mon.boost("def", 1))

    def _on_switch_out(self, side: Side, mon: Pokemon) -> None:
        if mon.has_ability("regenerator") and not mon.fainted:
            healed = mon.heal(mon.max_hp // 3)
            if healed:
                self.log.append(f"{mon.display} 的再生力回复了 {healed} HP。")
        if mon.has_ability("natural-cure") and mon.status:
            mon.status = ""
            mon.status_turns = 0
            self.log.append(f"{mon.display} 的自然回复治愈了异常状态。")
        mon.stages = {}
        mon.volatiles = {}
        mon.choice_locked = ""

    def _apply_hazards(self, side: Side, mon: Pokemon) -> None:
        if mon.has_ability("levitate") or (ITEMS.get(mon.item) or {}).get("effect", {}).get("ground_immune"):
            grounded = False
        else:
            grounded = "Flying" not in mon.types
        eff = (ITEMS.get(mon.item) or {}).get("effect") or {}
        if eff.get("no_hazards"):
            return
        hz = side.hazards
        if "stealthrock" in hz:
            mult = get_dex().type_multiplier("Rock", mon.types)
            dmg = max(1, int(mon.max_hp * mult / 8))
            mon.take_damage(dmg)
            self.log.append(f"隐形岩扎伤了 {mon.display}!({dmg})")
        if "spikes" in hz and grounded:
            layers = min(3, int(hz.get("spikes", 1)))
            dmg = max(1, int(mon.max_hp * (1 / 8 if layers == 1 else 1 / 6 if layers == 2 else 1 / 4)))
            mon.take_damage(dmg)
            self.log.append(f"撒菱扎伤了 {mon.display}!({dmg})")
        if "toxicspikes" in hz and grounded and not mon.status:
            layers = min(2, int(hz.get("toxicspikes", 1)))
            if (
                "Poison" in mon.types
                or "Steel" in mon.types
                or mon.has_ability("immunity")
            ):
                pass
            else:
                mon.status = "tox" if layers >= 2 else "psn"
                mon.status_turns = 0
                self.log.append(f"{mon.display} 踩到毒菱,陷入了{STATUS_ZH[mon.status]}!")
        if "stickyweb" in hz and grounded:
            msg = mon.boost("spe", -1)
            if msg:
                self.log.append(msg)

    # ── 太晶化 ──
    def _terastallize(self, side: Side) -> None:
        if side.tera_used:
            return
        mon = side.mon
        if mon is None or mon.terastallized or mon.fainted:
            return
        mon.terastallized = True
        side.tera_used = True
        tag = "我方" if side is self.player else "对方"
        self.log.append(
            f"{tag} {mon.display} 太晶化了!属性变为{get_dex().type_label(mon.tera_type)}!"
        )
        if mon.tera_type == "Stellar":
            self.log.append("星晶太晶:保留原属性防御,招式获得星晶之力。")
        if mon.has_ability("teraform-zero"):
            self.weather = ""
            self.terrain = ""
            self.log.append(f"{mon.display} 的归零化境消除了天气与场地!")

    # ── 出招 ──
    def _execute_move(self, side: Side, foe_side: Side, action: dict) -> None:
        mon = side.mon
        foe = foe_side.mon
        if mon is None or foe is None or mon.fainted or foe.fainted:
            return
        if mon.volatiles.get("flinch"):
            self.log.append(f"{mon.display} 畏缩了,无法行动!")
            return
        if not self._pre_move_status(mon):
            return

        move_key = action.get("move", "")
        entry = get_dex().moves.get(move_key)
        if entry is None:
            self.log.append(f"{mon.display} 想使出的招式不存在。")
            return
        if move_key not in mon.moves:
            self.log.append(f"{mon.display} 不会使用「{self._move_zh(move_key)}」!")
            return
        if mon.choice_locked and mon.choice_locked != move_key:
            self.log.append(f"{mon.display} 被讲究道具锁定,只能使用 {self._move_zh(mon.choice_locked)}!")
            move_key = mon.choice_locked
            entry = get_dex().moves.get(move_key) or entry
        if entry.get("category") == "Status" and (ITEMS.get(mon.item) or {}).get("effect", {}).get("no_status_moves"):
            self.log.append(f"{mon.display} 因突击背心无法使用变化招式!")
            return

        # PP 检查:耗尽则拒绝;全部耗尽则强制搏命
        cur_pp = mon.pp.get(move_key)
        if cur_pp is None:
            cur_pp = int(entry.get("pp", 10) or 10)
        if cur_pp <= 0:
            dex = get_dex()
            others = [
                m
                for m in mon.moves
                if m != move_key
                and int(mon.pp.get(m, (dex.moves.get(m) or {}).get("pp", 10)) or 0) > 0
            ]
            if others and not mon.choice_locked:
                self.log.append(
                    f"{self._move_zh(move_key)} 的 PP 已耗尽!{mon.display} 无法使出这一招。"
                )
                return
            self.log.append(f"{mon.display} 的招式 PP 全部耗尽,只能拼命了!")
            move_key = "struggle"
            entry = dex.moves["struggle"]

        # 混乱
        if mon.volatiles.get("confusion"):
            if self._rng(2).random() < 1 / 3:
                self.log.append(f"{mon.display} 混乱了,打伤了自己!")
                self_dmg = _calc_confusion_damage(self, mon)
                mon.take_damage(self_dmg)
                self.log.append(f"{mon.display} 受到了 {self_dmg} 点伤害。")
                self._dec_volatile(mon, "confusion")
                return
            self.log.append(f"{mon.display} 虽然混乱,但还是使出了招式!")
            self._dec_volatile(mon, "confusion")

        self.log.append(f"{mon.display} 使用了 {self._move_zh(move_key)}!")

        # PP
        self._spend_pp(mon, move_key)

        # 保护
        if foe.volatiles.get("protect") and entry.get("target") not in ("self", "allySide", "allyTeam"):
            self.log.append(f"{foe.display} 保护了自己,挡住了攻击!")
            return

        # 特性免疫(先于命中判定:免疫不吃命中)
        if self._ability_immunity(mon, foe, entry):
            return
        if (
            foe.has_ability("wonder-guard")
            and entry.get("category") != "Status"
            and get_dex().type_multiplier(self._move_type(mon, entry, move_key), foe.types) <= 1
        ):
            self.log.append(f"{foe.display} 的魔法守护挡住了攻击!")
            return

        # 命中判定
        if not self._accuracy_check(mon, foe, entry):
            self.log.append("但是没有命中!")
            return

        # 结算
        if entry.get("category") == "Status" or _is_fixed_damage(move_key) or entry.get("basePower", 0):
            self._resolve_effect(side, foe_side, mon, foe, move_key, entry)
        else:
            self._resolve_status_move(side, foe_side, mon, foe, move_key, entry)

        # 附带效果
        if entry.get("category") != "Status":
            self._apply_secondary(side, foe_side, mon, foe, move_key, entry)

        if mon.has_ability("sheer-force") or (ITEMS.get(mon.item) or {}).get("effect", {}).get("no_secondary"):
            pass  # 追加效果已在 _apply_secondary 内部按需忽略

        if mon.cur_hp <= 0:
            mon.fainted = True

    # ── 道具 / 捕获 / 逃走 ──
    @staticmethod
    def _who(side: Side) -> str:
        return "我方" if side.name == "player" else "对方"

    def _consume(self, side: Side, key: str) -> bool:
        """从背包扣除一件道具;返回是否成功。"""
        if side is not self.player:
            return True
        if int(self.bag.get(key, 0)) <= 0:
            return False
        self.bag[key] = int(self.bag[key]) - 1
        if self.bag[key] <= 0:
            del self.bag[key]
        return True

    def _execute_battle_item(self, side: Side, foe_side: Side, action: dict) -> None:
        key = action.get("item") or ""
        entry = BAG_ITEMS.get(key)
        if entry is None or entry.get("kind") == "ball":
            self.log.append(f"{self._who(side)} 对道具的使用没有效果。")
            return
        if not self._consume(side, key):
            self.log.append(f"背包里没有 {entry['zh']} 了。")
            return
        target = side.mon
        if action.get("target") is not None:
            try:
                idx = int(action["target"])
            except (TypeError, ValueError):
                idx = -1
            if 0 <= idx < len(side.party):
                target = side.party[idx]
        if target is None:
            return
        self.log.append(f"{self._who(side)} 使用了 {entry['zh']}!")
        self._apply_item_effect(target, entry.get("effect") or {}, action.get("move") or "")

    def _apply_item_effect(self, mon: Pokemon, eff: dict, move_key: str = "") -> None:
        if mon.fainted:
            if eff.get("revive_full") or eff.get("revive"):
                self._revive(mon, 1.0 if eff.get("revive_full") else float(eff.get("revive", 0.5)))
                self.log.append(f"{mon.display} 恢复了战斗能力!")
            else:
                self.log.append("但是没有效果……")
            return
        if eff.get("heal_full"):
            healed = mon.heal(mon.max_hp)
            self.log.append(f"{mon.display} 的 HP 完全回复了!({healed})")
        elif eff.get("heal_hp"):
            healed = mon.heal(int(eff["heal_hp"]))
            self.log.append(f"{mon.display} 回复了 {healed} HP。")
        elif eff.get("heal_hp_frac"):
            healed = mon.heal(max(1, int(mon.max_hp * float(eff["heal_hp_frac"]))))
            self.log.append(f"{mon.display} 回复了 {healed} HP。")
        cure = eff.get("cure_status")
        if cure and mon.status and (cure is True or mon.status in cure):
            old = STATUS_ZH.get(mon.status, mon.status)
            mon.status = ""
            mon.status_turns = 0
            self.log.append(f"{mon.display} 的{old}被治愈了!")
        if eff.get("pp_restore_all") or eff.get("pp_restore"):
            full = bool(eff.get("pp_restore_all"))
            pp_all = bool(eff.get("pp_all"))
            targets = list(mon.moves)
            if not pp_all:
                # 单招回复:优先指定招式,否则第一个 PP 未满的招式
                if move_key and move_key in mon.moves:
                    targets = [move_key]
                else:
                    targets = [
                        m
                        for m in mon.moves
                        if int(mon.pp.get(m, 0))
                        < int((get_dex().moves.get(m) or {}).get("pp", 10) or 10)
                    ][:1] or mon.moves[:1]
            amount = 0 if full else int(eff.get("pp_restore") or 0)
            for mv in targets:
                mx = int((get_dex().moves.get(mv) or {}).get("pp", 10) or 10)
                mon.pp[mv] = mx if full else min(mx, int(mon.pp.get(mv, mx)) + amount)
            self.log.append(
                f"{mon.display} 的全部招式 PP 完全回复了!"
                if pp_all and full
                else f"{mon.display} 回复了招式的 PP。"
            )
        for stat, stages in (eff.get("stat_boost") or {}).items():
            msg = mon.boost(stat, int(stages))
            if msg:
                self.log.append(msg)
        if eff.get("focus_energy"):
            mon.volatiles["focusenergy"] = 1
            self.log.append(f"{mon.display} 进入了易击中要害的状态!")
        if eff.get("guard_spec"):
            mon.volatiles["guardspec"] = 1
            self.log.append(f"{mon.display} 受到会心一击的概率降低了!")

    @staticmethod
    def _revive(mon: Pokemon, frac: float = 0.5) -> None:
        mon.cur_hp = max(1, int(mon.max_hp * frac)) if frac < 1 else mon.max_hp
        mon.fainted = False
        mon.faint_logged = False
        mon.status = ""
        mon.status_turns = 0

    def _catch_rate(self, mon: Pokemon) -> int:
        e = mon.entry
        if "captureRate" in e:
            return int(e["captureRate"])
        base = e.get("baseSpecies")
        if base:
            r = get_dex().resolve_species(base)
            if r and "captureRate" in r[1]:
                return int(r[1]["captureRate"])
        return 45

    def _ball_bonus(self, entry: dict, mon: Pokemon) -> float:
        eff = entry.get("effect") or {}
        bonus = float(eff.get("ball_bonus", 1.0))
        tags = set(mon.entry.get("tags") or [])
        types = set(mon.types)
        if eff.get("ball_net") and ({"Water", "Bug"} & types):
            bonus *= float(eff["ball_net"])
        if eff.get("ball_quick") and self.turn <= 1:
            bonus *= float(eff["ball_quick"])
        if eff.get("ball_timer"):
            bonus *= min(4.0, 1.0 + 0.1 * self.turn)
        if eff.get("ball_repeat"):
            bonus *= float(eff["ball_repeat"])
        if eff.get("ball_nest") or eff.get("ball_level"):
            lv = mon.level
            bonus *= 4.0 if lv < 20 else 3.0 if lv < 25 else 2.0 if lv < 30 else 1.0
        if eff.get("ball_heavy"):
            w = float(mon.entry.get("weightkg", 10) or 10)
            bonus *= 4.0 if w >= 200 else 2.0 if w >= 100 else 1.0
        if eff.get("ball_beast") and ("Ultra Beast" in tags or "Paradox" in tags):
            bonus *= float(eff["ball_beast"])
        if eff.get("ball_dusk"):
            bonus *= float(eff["ball_dusk"])
        return bonus

    def _execute_catch(self, side: Side, foe_side: Side, action: dict) -> None:
        if not self.wild or side is not self.player:
            self.log.append("只有在野生对战中才能投掷精灵球。")
            return
        mon = foe_side.mon
        if mon is None:
            return
        key = action.get("item") or "poke-ball"
        entry = BAG_ITEMS.get(key)
        if entry is None or entry.get("kind") != "ball":
            self.log.append("这不是精灵球。")
            return
        if not self._consume(side, key):
            self.log.append(f"背包里没有 {entry['zh']} 了。")
            return
        self.log.append(f"投出了 {entry['zh']}!")
        eff = entry.get("effect") or {}
        rate = self._catch_rate(mon)
        bonus = self._ball_bonus(entry, mon)
        status = (
            2.5
            if mon.status in ("slp", "frz")
            else 1.5
            if mon.status in ("brn", "par", "psn", "tox")
            else 1.0
        )
        if eff.get("ball_master"):
            caught, shakes = True, 3
        else:
            a = ((3 * mon.max_hp - 2 * mon.cur_hp) * rate * bonus) / (3 * mon.max_hp) * status
            if a >= 255:
                caught, shakes = True, 3
            else:
                b = 65536 / ((255 / a) ** 0.1875) if a > 0 else 0.0
                rng = self._rng(20)
                shakes = sum(1 for _ in range(4) if rng.random() * 65536 < b)
                caught = shakes == 4
        if caught:
            if eff.get("ball_heal"):
                mon.full_heal()
            self.captured = mon.to_dict()
            self.log.append(f"恭喜!成功捕获了 {mon.display}!")
            self.finished = True
            self.winner = "player"
        else:
            self.log.append(f"精灵球摇晃了 {shakes} 次……")
            self.log.append(f"{mon.display} 挣脱了精灵球!")

    def _execute_run(self, side: Side, foe_side: Side) -> None:
        if not self.wild:
            self.log.append("无法从与训练家的对战中逃走!")
            return
        mon, foe = side.mon, foe_side.mon
        if mon is None or foe is None:
            return
        ps = mon.battle_stat("spe", self)
        es = foe.battle_stat("spe", self)
        odds = ps * 32 // max(1, (es // 4) % 256) + 30 * self.run_attempts
        rng = self._rng(21)
        if odds > 255 or rng.randrange(256) < odds:
            self.log.append("成功逃走了!")
            self.finished = True
            self.escaped = True
            self.winner = "player"
        else:
            self.run_attempts += 1
            self.log.append("没能逃掉!")

    def _pre_move_status(self, mon: Pokemon) -> bool:
        """处理睡眠 / 冰冻 / 麻痹,返回是否可以行动。"""
        if mon.status == "slp":
            if mon.status_turns <= 0:
                mon.status = ""
                self.log.append(f"{mon.display} 醒来了!")
            else:
                mon.status_turns -= 1
                self.log.append(f"{mon.display} 正在睡觉,无法行动。")
                return False
        if mon.status == "frz":
            if self._rng(3).random() < 0.2:
                mon.status = ""
                self.log.append(f"{mon.display} 融化了!")
            else:
                self.log.append(f"{mon.display} 被冰冻住,无法行动。")
                return False
        if mon.status == "par" and self._rng(4).random() < 0.25:
            self.log.append(f"{mon.display} 因麻痹而无法行动!")
            return False
        return True

    def _spend_pp(self, mon: Pokemon, move_key: str) -> None:
        cur = mon.pp.get(move_key)
        if cur is None:
            entry = get_dex().moves.get(move_key) or {}
            mon.pp[move_key] = int(entry.get("pp", 10))
            cur = mon.pp[move_key]
        if cur > 0:
            mon.pp[move_key] = cur - 1
        mon.last_move = move_key
        eff = (ITEMS.get(mon.item) or {}).get("effect") or {}
        if eff.get("choice_lock") and not mon.choice_locked:
            mon.choice_locked = move_key

    def _accuracy_check(self, mon: Pokemon, foe: Pokemon, entry: dict) -> bool:
        acc = entry.get("accuracy", True)
        if acc is True or acc is None:
            return True
        if not isinstance(acc, (int, float)):
            return True
        # 一击必杀
        if entry.get("ohko"):
            return self._rng(5).random() < 0.3
        base = float(acc) / 100
        if mon.has_ability("hustle") and entry.get("category") == "Physical":
            base *= 0.8
        base *= _acc_mult(mon.stage("accuracy")) / _acc_mult(foe.stage("evasion"))
        # 复合眼 / 无防守
        if mon.has_ability("compound-eyes"):
            base *= 1.3
        if mon.has_ability("no-guard") or foe.has_ability("no-guard"):
            return True
        if foe.volatiles.get("telekinesis"):
            return True
        return self._rng(6).random() < min(1.0, base)

    def _ability_immunity(self, mon: Pokemon, foe: Pokemon, entry: dict) -> bool:
        """返回 True 表示被特性免疫打断。"""
        if mon.has_ability("mold-breaker", "teravolt", "turboblaze"):
            return False
        if entry.get("category") == "Status":
            return False
        mtype = self._move_type(mon, entry, mon.last_move)
        table = {
            "levitate": ("Ground", None, None),
            "flash-fire": ("Fire", "spa", 1),
            "well-baked-body": ("Fire", "def", 2),
            "water-absorb": ("Water", "heal", 0.25),
            "dry-skin": ("Water", "heal", 0.25),
            "storm-drain": ("Water", "spa", 1),
            "volt-absorb": ("Electric", "heal", 0.25),
            "lightning-rod": ("Electric", "spa", 1),
            "motor-drive": ("Electric", "spe", 1),
            "sap-sipper": ("Grass", "atk", 1),
            "earth-eater": ("Ground", "heal", 0.25),
        }
        for ab, (itype, act, val) in table.items():
            if foe.has_ability(ab) and mtype == itype:
                label = get_dex().type_label(itype)
                if act == "heal":
                    healed = foe.heal(max(1, int(foe.max_hp * val)))
                    self.log.append(
                        f"{foe.display} 的{foe.ability_name}吸收了{label}招式,回复了 {healed} HP!"
                    )
                elif act:
                    self.log.append(
                        f"{foe.display} 的{foe.ability_name}发动了!"
                    )
                    self.log.append(foe.boost(act, val))
                else:
                    self.log.append(f"{foe.display} 的{foe.ability_name}使{label}招式无效!")
                return True
        return False

    def _move_type(self, mon: Pokemon, entry: dict, move_key: str) -> str:
        """招式实际属性(含天气球 / 太晶爆发 / 太晶化)。"""
        key = move_key or mon.last_move
        if key == "terablast" and mon.terastallized:
            return mon.tera_type or entry.get("type", "Normal")
        if key in ("weatherball",):
            return {
                "sun": "Fire",
                "rain": "Water",
                "sand": "Rock",
                "snow": "Ice",
            }.get(self.weather, "Normal")
        if key == "terrainpulse":
            return {
                "electricterrain": "Electric",
                "grassyterrain": "Grass",
                "mistyterrain": "Fairy",
                "psychicterrain": "Psychic",
            }.get(self.terrain, "Normal")
        if key == "hiddenpower":
            return entry.get("type", "Normal")
        return entry.get("type", "Normal")

    def _move_zh(self, move_key: str) -> str:
        m = get_dex().moves.get(move_key) or {}
        return m.get("zh") or m.get("name") or move_key

    # ── 伤害 / 效果 ──
    def _resolve_effect(self, side, foe_side, mon, foe, move_key, entry) -> None:
        dex = get_dex()
        category = entry.get("category", "Physical")
        if category == "Status":
            self._resolve_status_move(side, foe_side, mon, foe, move_key, entry)
            return

        # 特殊固定伤害
        special = self._fixed_damage(mon, foe, move_key)
        if special is not None:
            dealt = foe.take_damage(special)
            self.log.append(f"{foe.display} 受到了 {dealt} 点伤害。")
            self._after_damage(side, foe_side, mon, foe, move_key, entry, dealt)
            return

        power = self._effective_power(mon, foe, move_key, entry)
        if power <= 0:
            self._resolve_status_move(side, foe_side, mon, foe, move_key, entry)
            return

        mtype = self._move_type(mon, entry, move_key)
        eff = dex.type_multiplier(mtype, foe.types)
        if eff == 0:
            self.log.append(f"对 {foe.display} 没有效果……")
            return

        crit = self._is_crit(mon, entry)
        damage = self._calc_damage(mon, foe, move_key, entry, power, mtype, eff, crit)
        # 气息腰带 / 结实 / 太晶壳
        if (
            damage >= foe.cur_hp
            and foe.cur_hp == foe.max_hp
            and (
                foe.has_ability("sturdy")
                or (
                    (ITEMS.get(foe.item) or {}).get("effect", {}).get("focus_sash")
                    and not foe.used_focus_sash
                )
            )
        ):
            damage = foe.cur_hp - 1
            if not foe.has_ability("sturdy"):
                foe.used_focus_sash = True
            self.log.append(f"{foe.display} 撑住了!留下 1 HP!")
        dealt = foe.take_damage(damage)
        self.log.append(
            f"击中 {foe.display}!"
            + (" 会心一击!" if crit else "")
            + f" 造成 {dealt} 点伤害。"
        )
        self._log_effectiveness(eff)

        # 吸血 / 反作用力
        drain = entry.get("drain")
        if drain:
            num, den = (drain if isinstance(drain, list) else [1, 2])[:2]
            healed = mon.heal(max(1, int(dealt * num / den)))
            if healed:
                self.log.append(f"{mon.display} 吸取了 {healed} HP!")
        recoil = entry.get("recoil")
        recoil_hp = entry.get("recoilMaxHp")
        if (recoil or recoil_hp) and not mon.has_ability("rock-head", "magic-guard"):
            frac = recoil_hp or recoil
            num, den = (frac if isinstance(frac, list) else [1, 3])[:2]
            base = mon.max_hp if recoil_hp else dealt
            rec = max(1, int(base * num / den))
            mon.take_damage(rec)
            self.log.append(f"{mon.display} 受到了 {rec} 点反作用力伤害。")
        life_orb = (ITEMS.get(mon.item) or {}).get("effect", {}).get("recoil")
        if life_orb and not mon.has_ability("magic-guard"):
            rec = max(1, int(mon.max_hp * life_orb))
            mon.take_damage(rec)
            self.log.append(f"{mon.display} 因生命宝珠损失了 {rec} HP。")

        self._after_damage(side, foe_side, mon, foe, move_key, entry, dealt)
        # 自身能力变化(如近身战 / 龙星群)
        for stat, delta in ((entry.get("self") or {}).get("boosts") or {}).items():
            msg = mon.boost(stat, int(delta))
            if msg:
                self.log.append(msg)
        if (entry.get("self") or {}).get("status"):
            self._inflict(mon, entry["self"]["status"])

    def _after_damage(self, side, foe_side, mon, foe, move_key, entry, dealt) -> None:
        # 对手被击中记录(用于报复类招式)
        if foe_side is self.player:
            self.player_damaged = True
        else:
            self.enemy_damaged = True
        # 凸凸头盔
        eff = (ITEMS.get(foe.item) or {}).get("effect") or {}
        flags = entry.get("flags") or []
        if eff.get("contact_recoil") and "contact" in flags and not mon.has_ability("magic-guard"):
            rec = max(1, int(mon.max_hp * eff["contact_recoil"]))
            mon.take_damage(rec)
            self.log.append(f"{mon.display} 被凸凸头盔反伤 {rec} HP!")
        # 弱点保险
        if (
            foe.cur_hp > 0
            and foe.item == "weakness-policy"
            and get_dex().type_multiplier(self._move_type(mon, entry, move_key), foe.types) > 1
            and not foe.volatiles.get("_wp")
        ):
            foe.volatiles["_wp"] = 1
            self.log.append(foe.boost("atk", 2))
            self.log.append(foe.boost("spa", 2))
        # 静电 / 火焰之躯等接触异常
        if "contact" in flags and foe.cur_hp > 0 and not mon.status:
            ab = foe.ability
            if ab == "static" and self._rng(7).random() < 0.3:
                self._inflict(mon, "par")
            elif ab == "flame-body" and self._rng(7).random() < 0.3:
                self._inflict(mon, "brn")
            elif ab == "poison-point" and self._rng(7).random() < 0.3:
                self._inflict(mon, "psn")
            elif ab == "effect-spore" and self._rng(7).random() < 0.3:
                self._inflict(mon, self._rng(8).choice(["par", "psn", "slp"]))
        # 爽喉喷雾
        if mon.item == "throat-spray" and "sound" in flags:
            self.log.append(mon.boost("spa", 1))

    def _log_effectiveness(self, eff: float) -> None:
        if eff >= 4:
            self.log.append("效果绝佳!")
        elif eff > 1:
            self.log.append("效果拔群!")
        elif 0 < eff < 1:
            self.log.append("效果不理想……")

    def _is_crit(self, mon: Pokemon, entry: dict) -> bool:
        ratio = int(entry.get("critRatio", 1) or 1)
        # 超幸运 / 狙击手
        if mon.has_ability("super-luck"):
            ratio += 1
        chances = {1: 1 / 24, 2: 1 / 8, 3: 1 / 2}
        p = chances.get(min(ratio, 3), 1 / 2)
        if mon.volatiles.get("focusenergy"):
            p = chances.get(min(ratio + 2, 3))
        return self._rng(9).random() < p

    def _calc_damage(
        self, mon: Pokemon, foe: Pokemon, move_key: str, entry: dict,
        power: float, mtype: str, eff: float, crit: bool,
    ) -> int:
        category = entry.get("category", "Physical")
        # 太晶爆发:太晶化后按攻/特攻较高者决定物理/特殊
        if move_key == "terablast" and mon.terastallized:
            category = (
                "Physical"
                if mon.battle_stat("atk", self) >= mon.battle_stat("spa", self)
                else "Special"
            )
        # 攻击 / 防御方能力(先确定数值来源,会心只作用于该数值)
        if move_key in ("bodypress",):
            atk_name, atk_owner = "def", mon
        elif move_key == "foulplay":
            atk_name, atk_owner = "atk", foe
        elif category == "Physical":
            atk_name, atk_owner = "atk", mon
        else:
            atk_name, atk_owner = "spa", mon

        if move_key in ("psyshock", "psystrike", "secretsword") or category == "Physical":
            def_name = "def"
        else:
            def_name = "spd"

        # 会心时忽略不利能力等级(作用于同一数值,不改变来源)
        if crit:
            atk_stat = _crit_offense(atk_owner, atk_name, self)
            def_stat = _crit_defense(foe, def_name, self)
        else:
            atk_stat = atk_owner.battle_stat(atk_name, self)
            def_stat = foe.battle_stat(def_name, self)

        level = mon.level
        base = math.floor(
            math.floor(math.floor(2 * level / 5 + 2) * power * atk_stat / def_stat) / 50
        ) + 2

        mods = 1.0
        # 天气
        if self.weather == "rain":
            if mtype == "Water":
                mods *= 1.5
            elif mtype == "Fire":
                mods *= 0.5
        elif self.weather == "sun":
            if mtype == "Fire":
                mods *= 1.5
            elif mtype == "Water":
                mods *= 0.5
        # 会心
        if crit:
            mods *= 1.5
        # 随机数
        mods *= self._rng(10).uniform(0.85, 1.0)
        # STAB / 太晶
        mods *= _stab_mult(mon, mtype, move_key)
        # 属性相克
        mods *= eff
        # 灼伤
        if category == "Physical" and mon.status == "brn" and not mon.has_ability("guts"):
            mods *= 0.5
        # 墙
        foe_side = self.enemy if foe is self.enemy.mon else self.player
        if not crit:
            if category == "Physical" and "reflect" in foe_side.screens:
                mods *= 0.5
            if category == "Special" and "lightscreen" in foe_side.screens:
                mods *= 0.5
            if "auroraveil" in foe_side.screens and self.weather == "snow":
                mods *= 0.5
        # 特性 / 道具修正
        mods *= _attacker_mods(
            self, mon, foe, move_key, entry, mtype, category, eff, power
        )
        mods *= _defender_mods(self, mon, foe, move_key, entry, mtype, category)

        return max(1, math.floor(base * mods))

    def _effective_power(self, mon, foe, move_key: str, entry: dict) -> float:
        power = float(entry.get("basePower", 0) or 0)
        targets_hp = foe.hp_frac()
        if move_key in ("lowkick", "grassknot"):
            w = float(foe.entry.get("weightkg", 10) or 10)
            table = [(10, 20), (25, 40), (50, 60), (100, 80), (200, 100)]
            power = 120
            for lim, p in table:
                if w <= lim:
                    power = p
                    break
        elif move_key == "gyroball":
            power = min(150, 25 * foe.battle_stat("spe", self) / max(1, mon.battle_stat("spe", self)) + 1)
        elif move_key == "electroball":
            ratio = mon.battle_stat("spe", self) / max(1, foe.battle_stat("spe", self))
            power = 40 if ratio < 1 else 60 if ratio < 2 else 80 if ratio < 3 else 120 if ratio < 4 else 150
        elif move_key in ("heavyslam", "heatcrash"):
            ratio = float(mon.entry.get("weightkg", 10) or 10) / max(
                0.1, float(foe.entry.get("weightkg", 10) or 10)
            )
            power = 40 if ratio < 2 else 60 if ratio < 3 else 80 if ratio < 4 else 100 if ratio < 5 else 120
        elif move_key in ("storedpower", "powertrip"):
            power = 20 + 20 * sum(v for v in mon.stages.values() if v > 0)
        elif move_key == "punishment":
            power = min(200, 60 + 20 * sum(v for v in foe.stages.values() if v > 0))
        elif move_key in ("flail", "reversal"):
            f = mon.hp_frac()
            power = 200 if f <= 1 / 48 else 150 if f <= 1 / 8 else 100 if f <= 1 / 4 else 80 if f <= 1 / 2 else 40 if f <= 0.6875 else 20
        elif move_key in ("eruption", "waterspout", "dragonenergy"):
            power = max(1.0, 150 * mon.hp_frac())
        elif move_key == "brine" and targets_hp <= 0.5 or move_key in ("hex", "infernalparade") and foe.status or move_key == "venoshock" and foe.status in ("psn", "tox"):
            power = 130
        elif move_key == "facade" and mon.status:
            power = 140
        elif move_key == "acrobatics" and not mon.item:
            power = 110
        elif move_key == "knockoff" and foe.item:
            power = 97.5
        elif move_key in ("boltbeak", "fishiousrend"):
            if mon.battle_stat("spe", self) >= foe.battle_stat("spe", self):
                power *= 2
        elif move_key in ("avalanche", "revenge") and (self.player_damaged if self.player.mon is mon else self.enemy_damaged) or move_key in ("payback", "assurance") and (self.player_damaged if self.player.mon is mon else self.enemy_damaged) or move_key == "weatherball" and self.weather or move_key == "terrainpulse" and self.terrain or move_key == "risingvoltage" and self.terrain == "electricterrain":
            power *= 2
        elif move_key == "terablast" and mon.terastallized:
            power = 100
        elif move_key == "expandingforce" and self.terrain == "psychicterrain" or move_key == "mistyexplosion" and self.terrain == "mistyterrain":
            power *= 1.5
        # 技术高手类由 _attacker_mods 处理
        return max(0.0, power)

    def _fixed_damage(self, mon, foe, move_key: str) -> int | None:
        if move_key in ("seismictoss", "nightshade"):
            return mon.level
        if move_key == "dragonrage":
            return 40
        if move_key == "sonicboom":
            return 20
        if move_key in ("superfang", "naturesmadness", "ruination"):
            return max(1, foe.cur_hp // 2)
        if move_key == "finalgambit":
            return mon.cur_hp
        if move_key == "endeavor" and mon.cur_hp < foe.cur_hp:
            return foe.cur_hp - mon.cur_hp
        if move_key == "psywave":
            return max(1, int(mon.level * self._rng(11).uniform(0.5, 1.5)))
        return None

    def _resolve_status_move(self, side, foe_side, mon, foe, move_key, entry) -> None:
        target = entry.get("target", "normal")
        to_self = target in ("self", "allySide", "allyTeam")

        # 恢复
        if entry.get("heal"):
            frac = entry["heal"]
            if isinstance(frac, list):
                frac = frac[0] / frac[1]
            healed = mon.heal(max(1, int(mon.max_hp * frac)))
            if healed:
                self.log.append(f"{mon.display} 回复了 {healed} HP!")
        if move_key == "rest" and mon.cur_hp < mon.max_hp:
            mon.cur_hp = mon.max_hp
            mon.status = "slp"
            mon.status_turns = 2
            self.log.append(f"{mon.display} 睡着了,HP 完全回复!")
        if move_key in ("healbell", "aromatherapy"):
            cured = []
            for p in side.party:
                if p.status:
                    cured.append(p.display)
                    p.status = ""
                    p.status_turns = 0
            if cured:
                self.log.append("治愈了 " + "、".join(cured) + " 的异常状态!")

        # 能力变化
        boosts = entry.get("boosts")
        if boosts:
            tgt = mon if to_self else foe
            for stat, delta in boosts.items():
                if stat in STAT_ORDER or stat in ("accuracy", "evasion"):
                    msg = tgt.boost(stat, int(delta))
                    if msg:
                        self.log.append(msg)

        # 异常状态
        if entry.get("status"):
            tgt = mon if to_self else foe
            self._inflict(tgt, entry["status"])
        if move_key == "willowisp":
            self._inflict(foe, "brn")
        if move_key == "thunderwave":
            if "Ground" in foe.types:
                self.log.append(f"{foe.display} 是地面属性,电磁波无效。")
            else:
                self._inflict(foe, "par")
        if move_key in ("toxic", "poisonpowder", "poisongas"):
            self._inflict(foe, "tox" if move_key == "toxic" else "psn")
        if move_key in ("sleeppowder", "spore", "hypnosis", "sing", "grasswhistle", "lovelykiss", "darkvoid"):
            self._inflict(foe, "slp")
        if move_key in ("glare", "stunspore"):
            self._inflict(foe, "par")

        # 变化状态
        vs = entry.get("volatileStatus")
        if vs:
            if vs == "flinch":
                if not to_self:
                    foe.volatiles["flinch"] = 1
                    self.log.append(f"{foe.display} 畏缩了!")
            elif vs == "confusion":
                foe.volatiles["confusion"] = self._rng(12).randint(2, 5)
                self.log.append(f"{foe.display} 陷入了混乱!")
            elif vs == "leechseed":
                if "Grass" not in foe.types:
                    foe.volatiles["leechseed"] = 1
                    self.log.append(f"{foe.display} 被种下了寄生种子!")
            elif vs == "protect":
                mon.volatiles["protect"] = 1
                self.log.append(f"{mon.display} 保护了自己!")
            elif vs == "taunt":
                foe.volatiles["taunt"] = int(entry.get("condition", {}).get("duration", 3) or 3)
                self.log.append(f"{foe.display} 被挑衅了!")
            elif vs == "focusenergy":
                mon.volatiles["focusenergy"] = 1
            elif vs == "substitute":
                self.log.append(f"{mon.display} 使用了替身(本引擎近似处理为无效果)。")

        # 场地 / 天气 / 陷阱 / 墙
        if entry.get("sideCondition"):
            self._set_side_condition(side, foe_side, mon, entry)
        if entry.get("weather"):
            self._set_field("weather", entry["weather"], mon)
        if entry.get("terrain"):
            self._set_field("terrain", entry["terrain"], mon)
        if entry.get("pseudoWeather"):
            self.field_effects[entry["pseudoWeather"]] = int(
                (entry.get("condition") or {}).get("duration", 5) or 5
            )
            self.log.append(f"{self._move_zh(move_key)} 生效了!")
        if move_key == "haze":
            for p in (mon, foe):
                p.reset_boosts()
            self.log.append("所有能力变化被清除!")
        if move_key in ("defog",):
            foe_side.hazards = {}
            side.hazards = {}
            foe_side.screens = {}
            self.log.append("清除了场上的陷阱与墙!")
        if move_key == "rapidspin":
            side.hazards = {}
            self.log.append("清除了我方场地陷阱!")
        if entry.get("selfSwitch"):
            self.log.append(f"{mon.display} 使出了换人招式(请下回合用 switch <序号>)。")
        if entry.get("selfdestruct"):
            mon.cur_hp = 0
            mon.fainted = True
            self.log.append(f"{mon.display} 倒下了!")

    def _inflict(self, target: Pokemon, status: str) -> None:
        if target.fainted or target.status:
            return
        # 属性 / 特性免疫
        if status == "brn" and "Fire" in target.types:
            self.log.append(f"{target.display} 是火属性,不会灼伤。")
            return
        if status == "frz" and "Ice" in target.types:
            self.log.append(f"{target.display} 是冰属性,不会冰冻。")
            return
        if status == "par" and "Electric" in target.types:
            self.log.append(f"{target.display} 是电属性,不会麻痹。")
            return
        if status in ("psn", "tox"):
            if "Poison" in target.types or "Steel" in target.types:
                self.log.append(f"{target.display} 不会中毒。")
                return
            if target.has_ability("immunity"):
                self.log.append(f"{target.display} 的免疫特性使其不会中毒!")
                return
        if status == "par" and target.has_ability("limber"):
            self.log.append(f"{target.display} 的柔软特性使其不会麻痹!")
            return
        if status == "slp" and target.has_ability("insomnia", "vital-spirit"):
            self.log.append(f"{target.display} 不会睡着!")
            return
        if status in ("brn",) and target.has_ability("water-veil", "water-bubble"):
            self.log.append(f"{target.display} 不会灼伤!")
            return
        if target.has_ability("magic-guard"):
            # 魔法防守只免异常状态造成的伤害,不免异常状态本身(在伤害结算处处理)
            pass
        if (ITEMS.get(target.item) or {}).get("effect", {}).get("cure_status"):
            self.log.append(f"{target.display} 的木子果治愈了异常状态!")
            return
        target.status = status
        if status == "slp":
            target.status_turns = self._rng(13).randint(1, 3)
        else:
            target.status_turns = 0
        self.log.append(f"{target.display} 陷入了{STATUS_ZH.get(status, status)}!")

    def _apply_secondary(self, side, foe_side, mon, foe, move_key, entry) -> None:
        eff = (ITEMS.get(mon.item) or {}).get("effect") or {}
        # 强颚 / 技术高手等附加效果的相关特性不阻止追加
        if mon.has_ability("sheer-force"):
            return
        if eff.get("no_secondary"):
            return
        if (ITEMS.get(foe.item) or {}).get("effect", {}).get("no_secondary"):
            return
        sec = entry.get("secondary")
        if not isinstance(sec, dict):
            return
        chance = float(sec.get("chance", 100) or 100)
        if self._rng(14).random() * 100 >= chance:
            return
        if sec.get("status"):
            self._inflict(foe, sec["status"])
        if sec.get("volatileStatus") == "flinch":
            foe.volatiles["flinch"] = 1
            self.log.append(f"{foe.display} 畏缩了!")
        elif sec.get("volatileStatus") == "confusion":
            foe.volatiles["confusion"] = self._rng(15).randint(2, 5)
            self.log.append(f"{foe.display} 陷入了混乱!")
        for stat, delta in (sec.get("boosts") or {}).items():
            msg = foe.boost(stat, int(delta))
            if msg:
                self.log.append(msg)
        for stat, delta in ((sec.get("self") or {}).get("boosts") or {}).items():
            msg = mon.boost(stat, int(delta))
            if msg:
                self.log.append(msg)

    def _set_side_condition(self, side, foe_side, mon, entry) -> None:
        cond = entry["sideCondition"]
        target = entry.get("target", "foeSide")
        tgt = side if target in ("allySide", "allyTeam") else foe_side
        if cond in ("reflect", "lightscreen", "auroraveil"):
            turns = 8 if mon.item == "light-clay" else 5
            tgt.screens[cond] = turns
            self.log.append(f"{SCREEN_ZH.get(cond, cond)} 展开了!")
        elif cond in HAZARD_ZH:
            if cond == "spikes":
                tgt.hazards[cond] = min(3, int(tgt.hazards.get(cond, 0)) + 1)
            elif cond == "toxicspikes":
                tgt.hazards[cond] = min(2, int(tgt.hazards.get(cond, 0)) + 1)
            else:
                tgt.hazards[cond] = 1
            self.log.append(f"{HAZARD_ZH.get(cond, cond)} 撒在了对方场地!")
        elif cond == "tailwind":
            tgt.screens["tailwind"] = 4
            self.log.append("顺风吹起了!")

    def _set_field(self, kind: str, value: str, mon: Pokemon) -> None:
        eff = (ITEMS.get(mon.item) or {}).get("effect") or {}
        if kind == "weather":
            turns = eff.get("weather_turns", 5) if eff.get("weather") == value else 5
            self.weather = value
            self.weather_turns = turns
            self.log.append(f"天气变成了{WEATHER_ZH.get(value, value)}!")
        else:
            turns = eff.get("terrain_turns", 5)
            self.terrain = value
            self.terrain_turns = turns
            self.log.append(f"场地变成了{TERRAIN_ZH.get(value, value)}!")

    # ── 回合结束 ──
    def _tick_end_of_turn(self) -> None:
        # 天气计数
        if self.weather:
            self.weather_turns -= 1
            if self.weather_turns <= 0:
                self.log.append(f"{WEATHER_ZH.get(self.weather, self.weather)}停止了。")
                self.weather = ""
        else:
            self.weather_turns = 0
        if self.terrain:
            self.terrain_turns -= 1
            if self.terrain_turns <= 0:
                self.log.append(f"{TERRAIN_ZH.get(self.terrain, self.terrain)}消失了。")
                self.terrain = ""
        for k in list(self.field_effects.keys()):
            self.field_effects[k] -= 1
            if self.field_effects[k] <= 0:
                del self.field_effects[k]

        for side in (self.player, self.enemy):
            for k in list(side.screens.keys()):
                if k == "tailwind":
                    side.screens[k] -= 1
                else:
                    side.screens[k] -= 1
                if side.screens[k] <= 0:
                    del side.screens[k]
            mon = side.mon
            if mon is None or mon.fainted:
                continue
            self._end_turn_mon(side, mon)

    def _end_turn_mon(self, side: Side, mon: Pokemon) -> None:
        if mon.fainted:
            return
        # 天气伤害
        eff = (ITEMS.get(mon.item) or {}).get("effect") or {}
        immune_wea = eff.get("no_weather_damage") or mon.has_ability("magic-guard", "overcoat")
        if self.weather in ("sand", "snow") and not immune_wea:
            if self.weather == "sand" and not (
                {"Rock", "Ground", "Steel"} & set(mon.types)
            ):
                dmg = max(1, mon.max_hp // 16)
                mon.take_damage(dmg)
                self.log.append(f"{mon.display} 受到沙暴伤害 {dmg}。")
            if self.weather == "snow" and "Ice" not in mon.types:
                dmg = max(1, mon.max_hp // 16)
                mon.take_damage(dmg)
                self.log.append(f"{mon.display} 受到下雪伤害 {dmg}。")
        if mon.fainted:
            return
        # 异常状态伤害
        mg = mon.has_ability("magic-guard")
        if mon.status == "brn":
            dmg = max(1, mon.max_hp // 16)
            if not mg:
                mon.take_damage(dmg)
                self.log.append(f"{mon.display} 受到灼伤伤害 {dmg}。")
        elif mon.status == "psn":
            dmg = max(1, mon.max_hp // 8)
            if not mg:
                mon.take_damage(dmg)
                self.log.append(f"{mon.display} 受到中毒伤害 {dmg}。")
        elif mon.status == "tox":
            mon.status_turns += 1
            dmg = max(1, mon.max_hp * min(15, mon.status_turns) // 16)
            if not mg:
                mon.take_damage(dmg)
                self.log.append(f"{mon.display} 受到剧毒伤害 {dmg}。")
        if mon.fainted:
            return
        # 道具回复
        healed = 0
        if mon.item == "leftovers":
            healed = mon.heal(max(1, mon.max_hp // 16))
        elif mon.item == "black-sludge":
            if "Poison" in mon.types:
                healed = mon.heal(max(1, mon.max_hp // 16))
            elif not mg:
                dmg = max(1, mon.max_hp // 8)
                mon.take_damage(dmg)
                self.log.append(f"{mon.display} 受到黑色污泥伤害 {dmg}。")
        if healed:
            self.log.append(f"{mon.display} 回复了 {healed} HP。")
        # 树果
        if mon.item == "sitrus-berry" and mon.hp_frac() <= 0.5:
            healed = mon.heal(max(1, mon.max_hp // 4))
            mon.item = ""
            self.log.append(f"{mon.display} 吃下文柚果,回复了 {healed} HP!")
        # 自我状态道具
        if mon.item == "toxic-orb" and not mon.status:
            self._inflict(mon, "tox")
        elif mon.item == "flame-orb" and not mon.status:
            self._inflict(mon, "brn")
        # 寄生种子
        if mon.volatiles.get("leechseed") and not mon.has_ability("magic-guard"):
            dmg = max(1, mon.max_hp // 8)
            mon.take_damage(dmg)
            foe_side = self.enemy if side is self.player else self.player
            foe = foe_side.mon
            if foe and not foe.fainted:
                foe.heal(dmg)
            self.log.append(f"{mon.display} 被寄生种子吸取了 {dmg} HP。")
        # 青草场地回复
        if self.terrain == "grassyterrain" and mon.hp_frac() < 1:
            grounded = "Flying" not in mon.types and not mon.has_ability("levitate")
            if grounded:
                healed = mon.heal(max(1, mon.max_hp // 16))
                if healed:
                    self.log.append(f"{mon.display} 因青草场地回复 {healed} HP。")

    def _check_faints(self) -> bool:
        any_faint = False
        # 循环处理“换上场后又被陷阱/天气打倒”的连环倒下
        for _ in range(12):
            round_faint = False
            for side, tag in ((self.player, "我方"), (self.enemy, "对方")):
                mon = side.mon
                if mon and mon.fainted and not mon.faint_logged:
                    mon.faint_logged = True
                    round_faint = True
                    any_faint = True
                    self.log.append(f"{tag} {mon.display} 倒下了!")
            if not round_faint:
                break
            for side in (self.player, self.enemy):
                mon = side.mon
                if mon and mon.fainted:
                    if not side.alive():
                        if not self.finished:
                            self._finish(side)
                    elif side is self.player:
                        self.awaiting_switch = True
                    else:
                        nxt = side.healthy()
                        if nxt:
                            self._send_out(side, nxt[0])
            if self.finished:
                break
        return any_faint

    def _finish(self, loser: Side) -> None:
        self.finished = True
        self.winner = "enemy" if loser is self.player else "player"
        self.log.append("我方全部宝可梦失去战斗能力……" if loser is self.player else "对方全部宝可梦失去战斗能力!")

    def _dec_volatile(self, mon: Pokemon, key: str) -> None:
        if key in mon.volatiles:
            mon.volatiles[key] = int(mon.volatiles[key]) - 1
            if mon.volatiles[key] <= 0:
                del mon.volatiles[key]

    # ── 简单 AI ──
    def ai_action(self) -> dict:
        side = self.enemy
        foe_side = self.player
        mon = side.mon
        foe = foe_side.mon
        if mon is None or foe is None or mon.fainted:
            return {"type": "move", "move": (mon.moves[0] if mon and mon.moves else "")}
        dex = get_dex()
        best: tuple[float, str] | None = None
        for mk in mon.moves:
            entry = dex.moves.get(mk)
            if entry is None:
                continue
            if int(mon.pp.get(mk, (entry.get("pp", 10) or 10)) or 0) <= 0:
                continue  # PP 已尽,不再选
            if entry.get("category") == "Status":
                score = 40.0
                if mk in ("protect",) and self.turn > 1:
                    score = 10.0
                if best is None or score > best[0]:
                    best = (score, mk)
                continue
            power = float(entry.get("basePower", 0) or 0) or 60
            mtype = self._move_type(mon, entry, mk)
            eff = dex.type_multiplier(mtype, foe.types)
            stab = 1.5 if mtype in mon.original_types else 1.0
            priority = int(entry.get("priority", 0) or 0)
            # 若可击杀,优先
            rough = power * eff * stab * (1 + 0.1 * priority)
            if eff > 1 and foe.hp_frac() < 0.35:
                rough *= 1.5
            if best is None or rough > best[0]:
                best = (rough, mk)
        move_key = best[1] if best else (mon.moves[0] if mon.moves else "")
        action: dict = {"type": "move", "move": move_key}
        # 太晶化:若太晶属性与招式属性一致且有增益,则启用
        if (
            not side.tera_used
            and mon.tera_type
            and self.turn >= 2
            and mon.tera_type in mon.original_types
            and foe.hp_frac() < 0.5
        ):
            action["tera"] = True
        return action

    def _ignores_ability(self, attacker: Pokemon) -> bool:
        return attacker.has_ability("mold-breaker", "teravolt", "turboblaze")


# ──────────────────────────── 辅助函数 ────────────────────────────


def _is_fixed_damage(move_key: str) -> bool:
    return move_key in {
        "seismictoss",
        "nightshade",
        "dragonrage",
        "sonicboom",
        "superfang",
        "naturesmadness",
        "ruination",
        "finalgambit",
        "endeavor",
        "psywave",
    }


def _stab_mult(mon: Pokemon, mtype: str, move_key: str) -> float:
    orig = mon.original_types
    if mon.terastallized:
        if mon.tera_type == "Stellar":
            # 星晶:保留原属性 STAB(1.5),并使原属性招式再获得 1.2 倍加成(近似实现)
            return 1.8 if mtype in orig else 1.0
        if mtype == mon.tera_type:
            return 2.0
        if mtype in orig:
            return 1.5
        return 1.0
    if mtype in orig:
        if mon.has_ability("adaptability"):
            return 2.0
        return 1.5
    return 1.0


def _ruin_modifier(battle: Battle, mon: Pokemon, stat: str) -> float:
    """灾祸系特性:降低对手对应数值(简化:按对手场上的宝可梦判定)。"""
    foe = battle.enemy.mon if battle.player.mon is mon else battle.player.mon
    if foe is None:
        return 1.0
    mapping = {
        "sword-of-ruin": "def",
        "tablets-of-ruin": "atk",
        "vessel-of-ruin": "spa",
        "beads-of-ruin": "spd",
    }
    for ab, affected in mapping.items():
        if foe.has_ability(ab) and stat == affected:
            return 0.75
    return 1.0


def _paradox_active(battle: Battle, mon: Pokemon, stat: str) -> bool:
    """古代/未来特性(或驱劲能量)是否提升该数值。按种族值最高的那项计算。"""
    if not (
        mon.has_ability("protosynthesis", "quark-drive")
        or (mon.item == "booster-energy")
    ):
        return False
    candidates = ["atk", "def", "spa", "spd", "spe"]
    boosted = max(candidates, key=lambda s: int(mon.stats.get(s, 0)))
    if mon.has_ability("quark-drive"):
        active = battle.terrain == "electricterrain"
    else:
        active = battle.weather == "sun"
    if mon.item == "booster-energy":
        active = True
    return active and stat == boosted


def _has_tailwind(battle: Battle, mon: Pokemon) -> bool:
    side = battle.player if battle.player.mon is mon else battle.enemy
    return "tailwind" in side.screens


def _crit_offense(mon: Pokemon, stat: str, battle: Battle) -> int:
    """会心时忽略自身的负向能力等级。"""
    saved = dict(mon.stages)
    mon.stages = {k: max(0, v) for k, v in saved.items()}
    val = mon.battle_stat(stat, battle)
    mon.stages = saved
    return val


def _crit_defense(mon: Pokemon, stat: str, battle: Battle) -> int:
    saved = dict(mon.stages)
    mon.stages = {k: min(0, v) for k, v in saved.items()}
    val = mon.battle_stat(stat, battle)
    mon.stages = saved
    return val


def _attacker_mods(
    battle: Battle, mon: Pokemon, foe: Pokemon, move_key: str, entry: dict,
    mtype: str, category: str, eff: float, power: float,
) -> float:
    mods = 1.0
    flags = set(entry.get("flags") or [])
    item_eff = (ITEMS.get(mon.item) or {}).get("effect") or {}
    if item_eff.get("damage_mult"):
        mods *= item_eff["damage_mult"]
    if item_eff.get("type_mult"):
        mods *= item_eff["type_mult"].get(mtype, 1.0)
    if item_eff.get("category_mult"):
        mods *= item_eff["category_mult"].get(category, 1.0)
    if item_eff.get("super_effective_mult") and eff > 1:
        mods *= item_eff["super_effective_mult"]

    if mon.has_ability("technician") and power <= 60:
        mods *= 1.5
    if mon.has_ability("tough-claws") and "contact" in flags:
        mods *= 1.3
    if mon.has_ability("punk-rock") and "sound" in flags:
        mods *= 1.3
    if mon.has_ability("steelworker", "steely-spirit") and mtype == "Steel":
        mods *= 1.5
    if mon.has_ability("transistor") and mtype == "Electric":
        mods *= 1.3
    if mon.has_ability("dragons-maw") and mtype == "Dragon":
        mods *= 1.5
    if mon.has_ability("rocky-payload") and mtype == "Rock":
        mods *= 1.5
    if mon.has_ability("water-bubble") and mtype == "Water":
        mods *= 2
    if mon.has_ability("iron-fist") and "punch" in flags:
        mods *= 1.2
    if mon.has_ability("strong-jaw") and "bite" in flags:
        mods *= 1.5
    if mon.has_ability("mega-launcher") and "pulse" in flags:
        mods *= 1.5
    if mon.has_ability("sharpness") and "slicing" in flags:
        mods *= 1.5
    if mon.has_ability("reckless") and entry.get("recoil"):
        mods *= 1.2
    if mon.has_ability("sheer-force") and entry.get("secondary"):
        mods *= 1.3
    if mon.has_ability("sand-force") and battle.weather == "sand" and mtype in ("Rock", "Ground", "Steel"):
        mods *= 1.3
    if mon.has_ability("analytic") and battle.turn > 1:
        mods *= 1.3
    if mon.has_ability("supreme-overlord"):
        fainted = sum(1 for p in (battle.player if battle.player.mon is mon else battle.enemy).party if p.fainted)
        mods *= 1 + 0.1 * fainted
    return mods


def _defender_mods(
    battle: Battle, mon: Pokemon, foe: Pokemon, move_key: str, entry: dict,
    mtype: str, category: str,
) -> float:
    mods = 1.0
    flags = set(entry.get("flags") or [])
    # 破格类忽略防御方特性
    ignore = mon.has_ability("mold-breaker", "teravolt", "turboblaze")
    if ignore:
        return mods
    if category == "Special" and foe.has_ability("ice-scales"):
        mods *= 0.5
    if category == "Physical" and foe.has_ability("fluffy") and "contact" in flags:
        mods *= 0.5
    if mtype == "Fire" and foe.has_ability("fluffy"):
        mods *= 2
    if mtype in ("Fire", "Ice") and foe.has_ability("thick-fat"):
        mods *= 0.5
    if mtype == "Fire" and foe.has_ability("heatproof"):
        mods *= 0.5
    if mtype == "Water" and foe.has_ability("water-bubble"):
        mods *= 0.5
    if foe.has_ability("filter", "solid-rock", "prism-armor") and get_dex().type_multiplier(mtype, foe.types) > 1:
        mods *= 0.75
    if foe.has_ability("multiscale", "shadow-shield") and foe.hp_frac() == 1:
        mods *= 0.5
    if foe.has_ability("tera-shell") and foe.hp_frac() == 1:
        mods *= 0.5
    if foe.has_ability("punk-rock") and "sound" in flags:
        mods *= 0.5
    return mods


def _calc_confusion_damage(battle: Battle, mon: Pokemon) -> int:
    # 40 威力、无属性的物理自伤(灼伤同样减半)
    atk = mon.battle_stat("atk", battle)
    dfn = mon.battle_stat("def", battle)
    base = math.floor(
        math.floor(math.floor(2 * mon.level / 5 + 2) * 40 * atk / dfn) / 50
    ) + 2
    mods = 1.0
    if mon.status == "brn" and not mon.has_ability("guts"):
        mods *= 0.5
    return max(1, int(base * mods * battle._rng(16).uniform(0.85, 1.0)))


# ──────────────────────────── 工厂函数 ────────────────────────────


def create_pokemon(
    species: str,
    level: int = 5,
    nickname: str = "",
    nature: str = "",
    ability: str = "",
    item: str = "",
    moves: list[str] | None = None,
    tera_type: str = "",
    ivs: dict | None = None,
    evs: dict | None = None,
    gender: str = "",
    friendship: int = 70,
) -> Pokemon:
    """按图鉴数据创建一只宝可梦。species 支持中英文名或标识。"""
    dex = get_dex()
    resolved = dex.resolve_species(species)
    if resolved is None:
        raise ValueError(f"未知宝可梦: {species}")
    key, entry = resolved
    level = max(1, min(100, int(level)))
    nature_key = dex.resolve_nature(nature) or "hardy"

    # 特性
    ability_key = ""
    if ability:
        a = dex.resolve_ability(ability)
        if a:
            ability_key = a[0]
    if not ability_key:
        default = (entry.get("abilities") or {}).get("0")
        if default:
            a = dex.resolve_ability(default)
            ability_key = a[0] if a else ""

    # 太晶属性
    tera = ""
    if tera_type:
        tera = dex.resolve_type(tera_type) or ""
    if not tera:
        tera = entry.get("requiredTeraType") or (entry.get("types") or ["Normal"])[0]

    # 招式
    move_keys: list[str] = []
    if moves:
        for m in moves:
            if not m:
                continue
            r = dex.resolve_move(m)
            if r and r[0] not in move_keys:
                move_keys.append(r[0])
    else:
        move_keys = dex.default_moveset(key, level)
    if not move_keys:
        move_keys = ["tackle"]

    ivs = dict(ivs or {})
    evs = dict(evs or {})
    stats = dex.compute_stats(key, level, ivs, evs, nature_key)
    item_key = ""
    if item:
        from .items import resolve_item

        r = resolve_item(item)
        item_key = r[0] if r else ""

    # 性别:优先固定性别字段,否则按性别比例随机
    gen = (gender or entry.get("gender") or "").strip().upper()[:1]
    if gen not in ("M", "F"):
        gr = entry.get("genderRate")
        if gr is None or int(gr) < 0:
            gen = ""
        elif int(gr) == 0:
            gen = "M"
        elif int(gr) >= 8:
            gen = "F"
        else:
            gen = "F" if random.random() < int(gr) / 8.0 else "M"

    mon = Pokemon(
        species=key,
        level=level,
        exp=dex.exp_for_level(dex.growth_of(key), level),
        nickname=nickname.strip(),
        nature=nature_key,
        ability=ability_key,
        item=item_key,
        moves=move_keys,
        tera_type=tera,
        gender=gen,
        friendship=max(0, min(255, int(friendship))),
        ivs=ivs,
        evs=evs,
        stats=stats,
        max_hp=stats["hp"],
        cur_hp=stats["hp"],
        pp={m: int((dex.moves.get(m) or {}).get("pp", 10) or 10) for m in move_keys},
    )
    return mon


def start_battle(
    player_party: list[Pokemon],
    enemy_party: list[Pokemon],
    *,
    weather: str = "",
    terrain: str = "",
    seed: int = 0,
    wild: bool = False,
    bag: dict | None = None,
) -> Battle:
    ok_weather = {
        "sun": "sun",
        "rain": "rain",
        "sand": "sand",
        "snow": "snow",
        "晴天": "sun",
        "下雨": "rain",
        "沙暴": "sand",
        "下雪": "snow",
    }
    weather = ok_weather.get(str(weather).strip(), "")
    terrain_map = {
        "electric": "electricterrain",
        "grassy": "grassyterrain",
        "misty": "mistyterrain",
        "psychic": "psychicterrain",
        "电气": "electricterrain",
        "青草": "grassyterrain",
        "薄雾": "mistyterrain",
        "精神": "psychicterrain",
    }
    terrain = terrain_map.get(str(terrain).strip(), "")
    battle = Battle(
        player=Side(name="player", party=player_party),
        enemy=Side(name="enemy", party=enemy_party),
        weather=weather,
        weather_turns=5 if weather else 0,
        terrain=terrain,
        terrain_turns=5 if terrain else 0,
        seed=int(seed or 0),
        wild=bool(wild),
        bag=dict(bag or {}),
    )
    # 首发选第一只未倒下的
    for side in (battle.player, battle.enemy):
        alive = side.healthy()
        side.active = alive[0] if alive else 0
    return battle


def battle_from_dict(d: dict) -> Battle:
    return Battle.from_dict(d)


def battle_to_dict(b: Battle) -> dict:
    return b.to_dict()


# 保持类型检查友好
__all__ = [
    "STATUS_ZH",
    "TERRAIN_ZH",
    "WEATHER_ZH",
    "Battle",
    "Pokemon",
    "Side",
    "battle_from_dict",
    "battle_to_dict",
    "create_pokemon",
    "start_battle",
]
