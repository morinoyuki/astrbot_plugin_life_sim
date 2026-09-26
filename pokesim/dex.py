"""内置宝可梦数据访问层(种族值 / 技能 / 招式学习 / 属性相克 / 特性 / 性格)。

数据由 `tools/build_pokemon_data.py` 生成,放在 `pokesim/static/*.json`,
运行时只读本地文件、不联网、不依赖第三方库。

对外主要入口:
    dex = get_dex()
    dex.resolve_species("皮卡丘")      -> ("pikachu", {...})
    dex.resolve_move("十万伏特")        -> ("thunderbolt", {...})
    dex.resolve_ability("静电")         -> ("static", {...})
    dex.type_multiplier("Electric", ["Water", "Flying"])  -> 4.0
    dex.compute_stats("pikachu", 50)    -> {"hp":..., "atk":..., ...}
    dex.learnable("pikachu", 30)        -> [{"move":..., "methods":..., "level":...}, ...]
"""

from __future__ import annotations

import difflib
import json
import os
import re
import unicodedata
from functools import cache, lru_cache

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

STAT_ORDER = ["hp", "atk", "def", "spa", "spd", "spe"]

# 归一化时要剔除的分隔符(便于「皮卡丘」「Pikachu」「Mr. Mime」「mr-mime」互查)
_SEP = set(" -_.·'’:\u3000()（）[]【】")


def _norm(text: str) -> str:
    if text is None:
        return ""
    # 全角→半角(数字/拉丁字母),让 "２０１号道路" 也能命中 "201号道路"
    s = unicodedata.normalize("NFKC", str(text).strip().lower())
    return "".join(ch for ch in s if ch not in _SEP)


# 遭遇方法分组(用于按环境筛选地点分布)
LAND_METHODS = {
    "walk",
    "grass-spots",
    "dark-grass",
    "cave-spots",
    "bridge-spots",
    "rough-terrain",
    "yellow-flowers",
    "purple-flowers",
    "red-flowers",
    "honey-tree",
    "berry-trees",
    "hidden-grotto",
    "rustling-bush-ambush",
    "trash-can-ambush",
    "ceiling-ambush",
    "ground-ambush",
    "horde",
    "sos",
    "sos-from-bubbling-spot",
    "overworld",
    "overworld-dirt",
}
WATER_METHODS = {
    "surf",
    "surf-spots",
    "seaweed",
    "bubbling-spots",
    "overworld-water",
}
FISH_METHODS = {
    "old-rod",
    "good-rod",
    "super-rod",
    "super-rod-spots",
    "feebas-tile-fishing",
}
ROCK_METHODS = {"rock-smash", "headbutt", "headbutt-low", "headbutt-normal", "headbutt-high"}
# 定点/赠予/群战外的特殊形式,默认不作为野生随机遭遇
SPECIAL_METHODS = {
    "static",
    "gift",
    "gift-egg",
    "npc-trade",
    "snag",
    "snag-rematch",
    "dynamax-adventure",
    "max-raid",
    "island-scan",
    "pokeflute",
    "devon-scope",
    "squirt-bottle",
    "wailmer-pail",
    "pokespot",
    "roaming-grass",
    "roaming-water",
    "wanderer",
    "wanderer-water",
    "chase-water",
    "overworld-flying",
    "overworld-special",
    "overworld-flying-special",
    "overworld-water-special",
    "pokemon-ranger",
}
LAND_GROUP = LAND_METHODS
WATER_GROUP = WATER_METHODS | FISH_METHODS


@cache
def _load(name: str) -> dict:
    path = os.path.join(DATA_DIR, f"{name}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class Dex:
    """宝可梦图鉴 / 招式 / 特性数据查询器(单例,线程内只读)。"""

    def __init__(self) -> None:
        self.species: dict[str, dict] = _load("species")
        self.moves: dict[str, dict] = _load("moves")
        # 搏命(PokeAPI/Showdown 已有条目):无属性克制、1/4 最大 HP 反作用
        self.moves.setdefault(
            "struggle",
            {
                "name": "Struggle",
                "zh": "挣扎",
                "category": "Physical",
                "basePower": 50,
                "accuracy": True,
                "pp": 1,
                "priority": 0,
            },
        )
        self.moves["struggle"].update(
            {"type": "???", "recoilMaxHp": [1, 4], "struggle": True}
        )
        self.learnsets: dict[str, dict] = _load("learnsets")
        self.learnset_gen: dict[str, int] = _load("learnset_gen")
        self.typechart: dict[str, dict[str, float]] = _load("typechart")
        self.natures: dict[str, dict] = _load("natures")
        self.abilities: dict[str, dict] = _load("abilities")
        self.meta: dict = _load("meta")
        self.type_zh: dict[str, str] = self.meta.get("type_zh", {})
        self.stat_zh: dict[str, str] = self.meta.get("stat_zh", {})
        self.zh_to_type: dict[str, str] = {
            _norm(v): k for k, v in self.type_zh.items()
        }
        # 索引:_norm(别名) -> 正式 key
        self._species_idx: dict[str, str] = {}
        for key, s in self.species.items():
            for alias in (key, s.get("name"), s.get("zh")):
                if alias:
                    self._species_idx.setdefault(_norm(alias), key)
        self._move_idx: dict[str, str] = {}
        for key, m in self.moves.items():
            for alias in (key, m.get("name"), m.get("zh")):
                if alias:
                    self._move_idx.setdefault(_norm(alias), key)
        self._ability_idx: dict[str, str] = {}
        for key, a in self.abilities.items():
            for alias in (key, a.get("name"), a.get("zh")):
                if alias:
                    self._ability_idx.setdefault(_norm(alias), key)

        try:
            locs = _load("locations")
        except (OSError, ValueError):
            locs = {}
        self.locations: dict[str, dict] = locs.get("locations", {})
        self.location_regions: dict[str, dict] = locs.get("regions", {})
        self.version_groups: dict[str, dict] = locs.get("versionGroups", {})
        self.location_methods: dict[str, str] = locs.get("methods", {})
        self._location_idx: dict[str, str] = {}
        for key, v in self.locations.items():
            aliases = [key, v.get("name"), v.get("zh"), *(v.get("aliases") or [])]
            for alias in aliases:
                if alias:
                    self._location_idx.setdefault(_norm(alias), key)

    # ─────────────── 通用解析 ───────────────

    @staticmethod
    def _resolve(query: str, table: dict, index: dict) -> tuple[str, dict] | None:
        if query is None:
            return None
        raw = str(query).strip()
        if not raw:
            return None
        if raw in table:
            return raw, table[raw]
        key = index.get(_norm(raw))
        if key:
            return key, table[key]
        # 模糊匹配(容错) — 只对长度足够的查询启用
        norm = _norm(raw)
        if len(norm) >= 2:
            close = difflib.get_close_matches(norm, index.keys(), n=1, cutoff=0.82)
            if close:
                key = index[close[0]]
                return key, table[key]
        return None

    def resolve_species(self, query: str) -> tuple[str, dict] | None:
        return self._resolve(query, self.species, self._species_idx)

    def resolve_move(self, query: str) -> tuple[str, dict] | None:
        return self._resolve(query, self.moves, self._move_idx)

    def resolve_ability(self, query: str) -> tuple[str, dict] | None:
        return self._resolve(query, self.abilities, self._ability_idx)

    def resolve_type(self, query: str) -> str | None:
        """把中英文属性名解析成规范英文属性名(Normal/Fire/…)。"""
        if not query:
            return None
        raw = str(query).strip()
        if raw in self.type_zh:
            return raw
        for t in self.type_zh:
            if t.lower() == raw.lower():
                return t
        return self.zh_to_type.get(_norm(raw))

    def resolve_nature(self, query: str) -> str | None:
        if not query:
            return None
        key = _norm(query)
        for k, v in self.natures.items():
            if key in (k, _norm(v.get("zh", "")), _norm(v.get("name", ""))):
                return k
        return None

    def type_label(self, t: str) -> str:
        """英文属性名 → 显示名(优先中文)。"""
        return self.type_zh.get(t, t)

    def stat_label(self, s: str) -> str:
        return self.stat_zh.get(s, s)

    # ─────────────── 属性相克 ───────────────

    def type_multiplier(self, attack_type: str, defender_types) -> float:
        """计算属性相克倍率(可叠加双属性)。返回 1.0 / 2.0 / 4.0 / 0.5 / 0.25 / 0。"""
        atk = self.resolve_type(attack_type) or attack_type
        if not atk:
            return 1.0
        if atk == "Stellar":
            # 星晶属性进攻对所有属性中性(引擎对特定目标另行处理)
            return 1.0
        chart = self.typechart.get(atk) or {}
        mult = 1.0
        for d in defender_types or []:
            dt = self.resolve_type(d) or d
            mult *= chart.get(dt, 1.0)
        return mult

    def effectiveness_label(self, mult: float) -> str:
        if mult == 0:
            return "无效(0×)"
        if mult >= 4:
            return "效果绝佳(4×)"
        if mult > 1:
            return "效果拔群(2×)"
        if mult <= 0.25:
            return "效果甚微(1/4×)"
        if mult < 1:
            return "效果不理想(1/2×)"
        return "效果一般(1×)"

    # ─────────────── 数值计算 ───────────────

    def compute_stats(
        self,
        species_key: str,
        level: int,
        ivs: dict | None = None,
        evs: dict | None = None,
        nature: str = "hardy",
    ) -> dict[str, int]:
        """按第 3 世代以后的公式算实际数值(不含战斗中变化)。"""
        entry = self.species.get(species_key)
        if entry is None:
            return dict.fromkeys(STAT_ORDER, 1)
        base = entry.get("baseStats", {})
        level = max(1, min(100, int(level)))
        nat = self.natures.get(self.resolve_nature(nature) or "hardy", {})
        inc, dec = nat.get("inc", ""), nat.get("dec", "")
        out: dict[str, int] = {}
        for stat in STAT_ORDER:
            b = int(base.get(stat, 1))
            iv = int((ivs or {}).get(stat, 31))
            ev = int((evs or {}).get(stat, 0))
            core = (2 * b + iv + ev // 4) * level // 100
            if stat == "hp":
                val = core + level + 10
            else:
                val = core + 5
                if inc == stat:
                    val = int(val * 1.1)
                elif dec == stat:
                    val = int(val * 0.9)
            out[stat] = max(1, val)
        return out

    # ─────────────── 招式学习 ───────────────

    def _lineage(self, species_key: str) -> list[str]:
        """返回该宝可梦的进化前系(自身 + prevo + baseSpecies),用于合并招式表。"""
        chain: list[str] = []
        seen: set[str] = set()
        queue = [species_key]
        while queue:
            key = queue.pop(0)
            if not key or key in seen:
                continue
            seen.add(key)
            chain.append(key)
            entry = self.species.get(key) or {}
            nxts = []
            if entry.get("prevo"):
                nxts.append(entry["prevo"])
            if entry.get("baseSpecies"):
                base = _norm(entry["baseSpecies"])
                if base != key:
                    nxts.append(base)
            for n in nxts:
                if n not in seen:
                    queue.append(n)
        return chain

    def learnset(self, species_key: str) -> dict[str, str]:
        """合并进化链上所有形态的第 9 世代(或该族最新世代)招式表。

        返回 {move_key: "L1,L5,M,T"}。等级招取最低等级,其余方法求并集。
        """
        merged: dict[str, set[str]] = {}
        for key in self._lineage(species_key):
            row = self.learnsets.get(key)
            if not row:
                continue
            for move, codes in row.items():
                merged.setdefault(move, set()).update(codes.split(","))
        out: dict[str, str] = {}
        for move, codes in merged.items():
            out[move] = ",".join(_sort_methods(codes))
        return out

    def learnable(
        self,
        species_key: str,
        level: int = 100,
        include_tm: bool = True,
        include_tutor: bool = True,
        include_egg: bool = False,
        include_event: bool = False,
    ) -> list[dict]:
        """列出该宝可梦可学招式。

        返回 [{move, key?, methods: ["L12","M"], level: 12|None}],按等级/名称排序。
        """
        row = self.learnset(species_key)
        out: list[dict] = []
        for move, codes in row.items():
            methods: list[str] = []
            min_level: int | None = None
            for c in codes.split(","):
                if not c:
                    continue
                if c.startswith("L"):
                    lv = int(c[1:] or 0)
                    # 进化招(L0)与已到等级
                    if lv <= level or lv == 0:
                        methods.append(c)
                    if min_level is None or lv < min_level:
                        min_level = lv
                elif c == "M" and include_tm or c == "T" and include_tutor or c == "E" and include_egg or c in ("S", "R", "V") and include_event:
                    methods.append(c)
            if methods:
                out.append({"move": move, "methods": methods, "level": min_level})
        out.sort(key=lambda x: (x["level"] is None, x["level"] or 0, x["move"]))
        return out

    def _own_learnset(self, species_key: str) -> dict[str, str]:
        """仅该形态自己的招式表(不含进化前),用于等级提升与默认配招。"""
        row = self.learnsets.get(species_key)
        if row:
            return row
        base = (self.species.get(species_key) or {}).get("baseSpecies")
        if base:
            r = self.resolve_species(base)
            if r:
                return self.learnsets.get(r[0], {}) or {}
        return {}

    def level_up_moves(self, species_key: str, old_level: int, new_level: int) -> list[str]:
        """返回在 (old_level, new_level] 区间内新学会的等级招(按等级升序)。"""
        row = self._own_learnset(species_key)
        got: list[tuple[int, str]] = []
        for move, codes in row.items():
            for c in codes.split(","):
                if c.startswith("L"):
                    lv = int(c[1:] or 0)
                    if old_level < lv <= new_level:
                        got.append((lv, move))
                        break
        got.sort()
        return [m for _, m in got]

    def level_moves(self, species_key: str, level: int) -> list[str]:
        """当前等级应已掌握的全部等级招(按习得等级升序,仅该形态自身)。"""
        row = self._own_learnset(species_key)
        got: list[tuple[int, str]] = []
        for move, codes in row.items():
            for c in codes.split(","):
                if c.startswith("L"):
                    lv = int(c[1:] or 0)
                    if lv <= level:
                        got.append((lv, move))
                        break
        got.sort()
        return [m for _, m in got]

    def default_moveset(
        self, species_key: str, level: int, n: int = 4, tm_fill: bool = True
    ) -> list[str]:
        """生成一套默认招式:优先最近的等级招,不足用可学招式补齐。"""
        picks: list[str] = []
        for m in reversed(self.level_moves(species_key, level)):
            if m not in picks:
                picks.append(m)
            if len(picks) >= n:
                break
        if tm_fill and len(picks) < n:
            for item in self.learnable(species_key, level):
                if item["move"] not in picks:
                    picks.append(item["move"])
                if len(picks) >= n:
                    break
        return picks[:n]

    def evolution(self, species_key: str, level: int) -> str | None:
        """若达到条件则返回进化后的形态 key(仅处理等级进化,等级记录在进化后形态上)。"""
        entry = self.species.get(species_key) or {}
        for e in entry.get("evos") or []:
            cand = self.species.get(e) or {}
            if cand.get("battleOnly"):
                continue
            if cand.get("prevo") and cand["prevo"] != species_key:
                continue
            lv = cand.get("evoLevel")
            if lv and level >= int(lv):
                return e
        return None

    # ─────────────── 进化判定 ───────────────

    FRIENDSHIP_EVO = 160

    @staticmethod
    def _norm_item(s: str | None) -> str:
        return "".join(ch for ch in str(s or "").lower() if ch.isalnum())

    def item_matches(self, user_item: str | None, evo_item: str | None) -> bool:
        if not user_item or not evo_item:
            return False
        a = self._norm_item(user_item)
        b = self._norm_item(evo_item)
        return bool(a) and (a == b or a in b or b in a)

    @staticmethod
    def _friendship_need(reason: str) -> int:
        m = re.search(r"(\d+)\s*(?:friendship|affection)", reason, re.IGNORECASE)
        if m:
            return int(m.group(1))
        return 160

    @staticmethod
    def _daytime_ok(reason: str, daytime: str | None) -> bool:
        low = reason.lower()
        needs_night = "night" in low or "midnight" in low or "full moon" in low
        needs_day = "during the day" in low or "daytime" in low
        if not needs_night and not needs_day:
            return True
        if not daytime:
            return False  # 需要昼夜信息才能判定
        if needs_night:
            return daytime == "night"
        return daytime == "day"

    @staticmethod
    def _stat_ok(reason: str, stats: dict | None) -> bool:
        if not stats:
            return True
        low = reason.lower()
        if "atk stat" in low and "def stat" in low:
            atk = int(stats.get("atk", 0))
            dfn = int(stats.get("def", 0))
            if "equal" in low:
                return atk == dfn
            if ">" in low:
                return atk > dfn
            if "<" in low:
                return atk < dfn
        return True

    def _move_type(self, move_key: str) -> str:
        return str((self.moves.get(move_key) or {}).get("type") or "")

    def evolution_options(
        self,
        species_key: str,
        *,
        level: int = 1,
        moves=( ),
        item: str | None = None,
        friendship: int = 70,
        gender: str = "",
        trade: bool = False,
        daytime: str | None = None,
        stats: dict | None = None,
    ) -> list[dict]:
        """列出当前状态下所有进化选项,每项 {target, kind, reason, met, ...}。"""
        entry = self.species.get(species_key) or {}
        moveset = set(moves or ())
        out: list[dict] = []
        for e in entry.get("evos") or []:
            ce = self.species.get(e) or {}
            if ce.get("battleOnly") or ce.get("isCosmeticForme"):
                continue
            kind = ce.get("evoType", "level") or "level"
            reason = ce.get("evoCondition") or ""
            req_level = int(ce.get("evoLevel") or 0)
            req_item = ce.get("evoItem")
            req_move = ce.get("evoMove")
            met = False
            if kind in ("level", ""):
                met = level >= (req_level or 1)
            elif kind == "levelFriendship":
                met = level >= (req_level or 1) and friendship >= self._friendship_need(reason)
            elif kind == "levelMove":
                rm = self.resolve_move(req_move) if req_move else None
                met = bool(rm) and rm[0] in moveset and level >= (req_level or 1)
            elif kind == "levelHold":
                met = self.item_matches(item, req_item) and level >= (req_level or 1)
                met = met and self._daytime_ok(reason, daytime)
            elif kind == "useItem":
                met = self.item_matches(item, req_item)
            elif kind == "trade":
                met = bool(trade) and (not req_item or self.item_matches(item, req_item))
            elif kind == "levelExtra":
                met = self._extra_met(ce, moveset, friendship)
            else:  # other:特殊条件,需手动 force
                met = False
            fixed = (ce.get("gender") or "").upper()[:1]
            if fixed in ("M", "F") and gender and gender != fixed:
                met = False
            # 昼夜限定:对所有进化方式都适用(无条件时为 no-op)
            met = met and self._daytime_ok(reason, daytime)
            met = met and self._stat_ok(reason, stats)
            low = reason.lower()
            if "female" in low and gender and gender != "F":
                met = False
            if re.search(r"\bmale\b", low) and gender and gender != "M":
                met = False
            out.append(
                {
                    "target": e,
                    "kind": kind,
                    "reason": reason,
                    "met": met,
                    "level": req_level,
                    "item": req_item,
                    "move": req_move,
                }
            )
        return out

    def _extra_met(self, ce: dict, moveset: set, friendship: int) -> bool:
        reason = (ce.get("evoCondition") or "").lower()
        if "fairy" in reason:
            return friendship >= 100 and any(
                self._move_type(m) == "Fairy" for m in moveset
            )
        # 磁场 / 特殊地点等:模拟中视为可达成(由叙事决定)
        return True

    def level_evolutions(
        self,
        species_key: str,
        *,
        level: int,
        moves=(),
        item: str | None = None,
        friendship: int = 70,
        gender: str = "",
        daytime: str | None = None,
        stats: dict | None = None,
    ) -> list[dict]:
        """升级时可自动触发的进化(等级/亲密度/招式/携带物/特殊)。"""
        kinds = {"level", "levelFriendship", "levelMove", "levelHold", "levelExtra"}
        opts = self.evolution_options(
            species_key,
            level=level,
            moves=moves,
            item=item,
            friendship=friendship,
            gender=gender,
            daytime=daytime,
            stats=stats,
        )
        return [o for o in opts if o["met"] and o["kind"] in kinds]

    def use_item_evolutions(
        self, species_key: str, item: str | None, gender: str = ""
    ) -> list[dict]:
        """使用道具可触发的进化(带性别限制)。"""
        return [
            o
            for o in self.evolution_options(species_key, item=item, gender=gender)
            if o["met"] and o["kind"] == "useItem"
        ]

    # ─────────────── 经验 / 成长曲线 ───────────────

    def growth_of(self, species_key: str) -> str:
        e = self.species.get(species_key) or {}
        if e.get("growthRate"):
            return e["growthRate"]
        base = e.get("baseSpecies")
        if base:
            r = self.resolve_species(base)
            if r and r[1].get("growthRate"):
                return r[1]["growthRate"]
        return "medium"

    def base_exp(self, species_key: str) -> int:
        e = self.species.get(species_key) or {}
        if e.get("baseExp"):
            return int(e["baseExp"])
        base = e.get("baseSpecies")
        if base:
            r = self.resolve_species(base)
            if r and r[1].get("baseExp"):
                return int(r[1]["baseExp"])
        return 60

    def exp_for_level(self, growth: str, level: int) -> int:
        """升到 `level` 级所需的累计经验(第 3 世代后曲线)。"""
        n = max(0, min(100, int(level)))
        if n <= 0:
            return 0
        if growth == "fast":
            val = 4 * n**3 / 5
        elif growth == "medium-slow":
            val = 6 * n**3 / 5 - 15 * n**2 + 100 * n - 140
        elif growth == "slow":
            val = 5 * n**3 / 4
        elif growth == "slow-then-very-fast":
            r = n % 3
            if n <= 50:
                val = n**3 * (100 - n) / 50
            elif n <= 68:
                val = n**3 * (150 - n) / 100
            elif n <= 98:
                val = n**3 * (1274 + r * r - 9 * r - 20 * (n // 3)) / 1000
            else:
                val = n**3 * (160 - n) / 100
        elif growth == "fast-then-very-slow":
            if n <= 15:
                val = n**3 * (24 + (n + 1) // 3) / 50
            elif n <= 35:
                val = n**3 * (14 + n) / 50
            else:
                val = n**3 * (32 + n // 2) / 50
        else:  # medium-fast
            val = n**3
        return max(0, int(val))

    def level_from_exp(self, growth: str, exp: int) -> int:
        """根据累计经验反推等级(1-100)。"""
        exp = max(0, int(exp))
        level = 1
        while level < 100 and self.exp_for_level(growth, level + 1) <= exp:
            level += 1
        return level

    def exp_yield(self, species_key: str, level: int, trainer: bool = False) -> int:
        """击倒一只宝可梦获得的经验值(第 5 世代后简化公式)。"""
        base = self.base_exp(species_key)
        lv = max(1, int(level))
        exp = base * lv // 7
        if trainer:
            exp = exp * 3 // 2
        return max(1, exp)

    # ─────────────── 地点 / 野外分布 ───────────────

    def _loc_in_region(self, key: str, region: str) -> bool:
        return not region or (self.locations.get(key) or {}).get("region") == region

    def resolve_region(self, query: str) -> str:
        """地区名/中文名/标识 → 地区 key(如 关都 → kanto)。"""
        if not query:
            return ""
        raw = _norm(query)
        if raw in self.location_regions:
            return raw
        for k, v in self.location_regions.items():
            if raw in (_norm(k), _norm(v.get("name", "")), _norm(v.get("zh", ""))):
                return k
        for k, v in self.location_regions.items():
            zh = _norm(v.get("zh", ""))
            if raw and zh and (raw in zh or zh in raw):
                return k
        return ""

    def resolve_location(self, query: str, region: str = "") -> tuple[str, dict] | None:
        if query is None:
            return None
        raw = str(query).strip()
        if not raw:
            return None
        region = self.resolve_region(region) or region
        if raw in self.locations and self._loc_in_region(raw, region):
            return raw, self.locations[raw]
        key = self._location_idx.get(_norm(raw))
        if key and self._loc_in_region(key, region):
            return key, self.locations[key]
        norm = _norm(raw)
        cands: list[str] = []
        for k, v in self.locations.items():
            if region and v.get("region") != region:
                continue
            names = [_norm(k), _norm(v.get("name", "")), _norm(v.get("zh", ""))]
            names += [_norm(a) for a in (v.get("aliases") or [])]
            if any(norm and n and (norm in n or n in norm) for n in names):
                cands.append(k)
        if not cands and region:
            return self.resolve_location(raw, "")
        if not cands:
            return None
        for k in cands:
            v = self.locations[k]
            if norm in (_norm(v.get("zh", "")), _norm(v.get("name", ""))):
                return k, v
        return cands[0], self.locations[cands[0]]

    def find_location(self, query: str, region: str = "") -> str:
        r = self.resolve_location(query, region)
        return r[0] if r else ""

    def location_default_vg(self, key: str) -> str:
        pools = (self.locations.get(key) or {}).get("pools") or {}
        if not pools:
            return ""
        return max(pools, key=lambda g: int((self.version_groups.get(g) or {}).get("order", 0)))

    def location_pools(
        self,
        key: str,
        version_group: str = "",
        methods: set[str] | None = None,
        include_special: bool = False,
    ) -> list[dict]:
        """返回地点遭遇条目 [{species,min,max,method,chance}]。

        methods 显式指定时只保留这些方法;否则默认排除 SPECIAL_METHODS。
        """
        v = self.locations.get(key) or {}
        pools = v.get("pools") or {}
        vg = version_group or self.location_default_vg(key)
        rows = pools.get(vg)
        if rows is None:
            rows = [r for rr in pools.values() for r in rr]
        out: list[dict] = []
        for skey, lo, hi, method, chance in rows or []:
            if methods is not None:
                if method not in methods:
                    continue
            elif not include_special and method in SPECIAL_METHODS:
                continue
            out.append(
                {"species": skey, "min": int(lo), "max": int(hi), "method": method, "chance": int(chance)}
            )
        return out

    def list_locations(self, region: str = "") -> list[str]:
        keys = [k for k, v in self.locations.items() if not region or v.get("region") == region]
        return sorted(keys, key=lambda k: (self.locations[k].get("region", ""), k))


def _sort_methods(codes: set[str]) -> list[str]:
    level = sorted(
        (c for c in codes if c.startswith("L")), key=lambda c: int(c[1:] or 0)
    )
    other = sorted(c for c in codes if not c.startswith("L"))
    return level + other


@lru_cache(maxsize=1)
def get_dex() -> Dex:
    return Dex()


# 便捷函数(避免到处 get_dex())
def resolve_species(query: str):
    return get_dex().resolve_species(query)


def resolve_move(query: str):
    return get_dex().resolve_move(query)


def type_multiplier(attack_type: str, defender_types) -> float:
    return get_dex().type_multiplier(attack_type, defender_types)
