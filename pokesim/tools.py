"""`poke_*` 工具:Mixin 给主插件,供 LLM 在「宝可梦模式」中查询数据与推演对战。

所有工具与 `rpg_*` 一样,通过 `_build_my_tool_set` 自动收集注册:
只需 `poke_` 前缀 + Google 风格 docstring。

状态存在独立文件 `<data_dir>/pokemon/<scope>.json`(见 `store.PokeStore`),
不写回 sim session,避免与 `_generate` 末尾的整体落库互相覆盖。
"""

from __future__ import annotations

import random
import re

from .dex import STAT_ORDER, _norm, get_dex
from .encounter import (
    detect_environment,
    roll_encounter,
    roll_level,
    roll_location_encounter,
)
from .engine import (
    STATUS_ZH,
    Battle,
    Pokemon,
    Side,
    battle_from_dict,
    battle_to_dict,
    create_pokemon,
    start_battle,
)
from .items import (
    BAG_ITEMS,
    KIND_ORDER,
    KIND_ZH,
    item_label,
    resolve_bag_item,
    resolve_item,
)
from .store import PokeStore
from .trainer import generate_team, team_to_enemy_string

MAX_PARTY = 6

EGG_ZH = {
    "monster": "怪兽",
    "water1": "水中1",
    "bug": "虫",
    "flying": "飞行",
    "ground": "陆上",
    "field": "陆上",
    "fairy": "妖精",
    "plant": "植物",
    "grass": "植物",
    "humanshape": "人形",
    "human-like": "人形",
    "water3": "水中3",
    "mineral": "矿物",
    "indeterminate": "不定形",
    "amorphous": "不定形",
    "water2": "水中2",
    "ditto": "百变怪",
    "dragon": "龙",
    "no-eggs": "未发现",
    "undiscovered": "未发现",
}

GENDER_ZH = {"M": "♂", "F": "♀"}

_STAT_ALIAS = {
    "攻击": "atk",
    "物攻": "atk",
    "防御": "def",
    "物防": "def",
    "特攻": "spa",
    "特防": "spd",
    "速度": "spe",
    "体力": "hp",
    "生命": "hp",
}


class PokemonMixin:
    """宝可梦工具集。宿主需在 __init__ 设置 `self.data_dir`。"""

    data_dir: str

    # ──────────────────────────── 基础设施 ────────────────────────────

    def _poke_store(self) -> PokeStore:
        store = getattr(self, "_poke_store_obj", None)
        if store is None:
            store = PokeStore(self.data_dir)
            self._poke_store_obj = store
        return store

    def _poke_scope(self, event) -> str:
        """每个玩家一支队伍:群聊=群号+用户 id(支持多玩家),私聊=用户 id。"""
        try:
            gid = str(event.get_group_id() or "")
        except Exception:
            gid = ""
        try:
            uid = str(event.get_sender_id() or "")
        except Exception:
            uid = ""
        if gid and uid:
            return f"group_{gid}_{uid}"
        if gid:
            return f"group_{gid}"
        return uid or "default"

    def _poke_load(self, event) -> dict:
        return self._poke_store().load(self._poke_scope(event))

    def _poke_save(self, event, data: dict) -> None:
        self._poke_store().save(self._poke_scope(event), data)

    # ── /undo 快照接口(由 main/rpg_tools 调用)──

    def pokemon_capture(self, event) -> dict:
        scope = self._poke_scope(event)
        store = self._poke_store()
        data = store.load(scope)
        has_state = bool(data.get("party") or data.get("box") or data.get("battle"))
        return {"scope": scope, "data": data if has_state else None}

    def pokemon_apply(self, state: dict) -> None:
        if not isinstance(state, dict):
            return
        scope = state.get("scope")
        if not scope:
            return
        data = state.get("data")
        store = self._poke_store()
        if data:
            store.save(scope, data)
        else:
            store.delete(scope)

    def pokemon_purge(self, event) -> bool:
        return self._poke_store().delete(self._poke_scope(event))

    # ── 队伍定位 ──

    @staticmethod
    def _party_of(data: dict) -> list[dict]:
        party = data.get("party")
        if not isinstance(party, list):
            party = []
            data["party"] = party
        return party

    def _find_member(self, data: dict, target: str) -> tuple[int, dict] | None:
        """按 1 基序号 / 昵称 / 宝可梦名(中英)定位队伍成员。"""
        party = self._party_of(data)
        raw = str(target or "").strip()
        if not raw:
            return None
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(party):
                return idx, party[idx]
            return None
        dex = get_dex()
        key = raw.lower()
        for i, p in enumerate(party):
            if (p.get("nickname") or "").strip() == raw:
                return i, p
            if p.get("species") == key:
                return i, p
        for i, p in enumerate(party):
            if dex.species.get(p.get("species", ""), {}).get("zh") == raw:
                return i, p
        # 模糊
        for i, p in enumerate(party):
            info = dex.species.get(p.get("species", ""), {})
            if key in (p.get("species", ""), (info.get("name") or "").lower(), info.get("zh") or ""):
                return i, p
        return None

    # ── 格式化 ──

    @staticmethod
    def _fmt_mon(mon: Pokemon, index: int | None = None) -> str:
        dex = get_dex()
        entry = mon.entry
        types = "/".join(dex.type_label(t) for t in mon.original_types)
        head = f"{index}. " if index else ""
        name = mon.display
        bits = [f"{head}{name} Lv{mon.level} [{types}]"]
        if mon.status:
            bits.append(STATUS_ZH.get(mon.status, mon.status))
        bits.append(f"HP {mon.cur_hp}/{mon.max_hp}")
        lines = [" ".join(bits)]
        stats = " ".join(
            f"{dex.stat_label(s)} {mon.stats.get(s, 0)}" for s in STAT_ORDER
        )
        lines.append(f"   {stats}")
        lines.append(
            f"   性格 {dex.natures.get(mon.nature, {}).get('zh', mon.nature)}"
            f" | 特性 {mon.ability_name}"
            + (f" | 道具 {item_label(mon.item)}" if mon.item else "")
            + f" | 太晶 {dex.type_label(mon.tera_type)}"
            + (f" | {GENDER_ZH.get(mon.gender, '')}" if mon.gender else "")
            + f" | 亲密度 {mon.friendship}"
        )
        if mon.level < 100:
            growth = dex.growth_of(mon.species)
            base = dex.exp_for_level(growth, mon.level)
            need = dex.exp_for_level(growth, mon.level + 1)
            lines.append(
                f"   经验 {mon.exp - base}/{need - base} (累计 {mon.exp})"
            )
        evs = " ".join(
            f"{dex.stat_label(s)}+{v}" for s, v in mon.evs.items() if v
        )
        if evs:
            lines.append(f"   努力值: {evs}")
        mv = []
        for i, m in enumerate(mon.moves, 1):
            info = dex.moves.get(m) or {}
            mv.append(
                f"{i}.{info.get('zh') or info.get('name') or m}"
                f"({info.get('type') and dex.type_label(info['type'])}·{info.get('basePower') or '—'}"
                f"·PP{mon.pp.get(m, info.get('pp', 0))}/{info.get('pp', 0)})"
            )
        lines.append("   招式: " + " ".join(mv))
        if entry.get("evos") and not entry.get("battleOnly"):
            evo_names = "、".join(
                dex.species.get(x, {}).get("zh", x) for x in entry["evos"]
            )
            lines.append("   可进化: " + evo_names)
        return "\n".join(lines)

    @staticmethod
    def _parse_moves(raw: str) -> list[str]:
        """解析招式/属性列表:支持多种分隔符,去重并限制为 4 个(宝可梦最多 4 招)。"""
        if not raw:
            return []
        for sep in ("、", "，", ",", "/", "|"):
            raw = raw.replace(sep, ",")
        out: list[str] = []
        seen: set[str] = set()
        for m in (x.strip() for x in raw.split(",")):
            if not m:
                continue
            key = _norm(m)
            if key in seen:
                continue
            seen.add(key)
            out.append(m)
            if len(out) >= 4:
                break
        return out

    # ──────────────────────────── 图鉴工具 ────────────────────────────

    async def poke_dex_species(self, event, name: str) -> str:
        """查询宝可梦图鉴:图鉴说明、属性、种族值、特性、进化、身高体重。

        Args:
            name(string): 宝可梦名称,支持中文/英文/标识,如 "皮卡丘" / "Pikachu" / "pikachu" / "喷火龙（Mega-X）"。
        """
        dex = get_dex()
        r = dex.resolve_species(name)
        if r is None:
            return f"❌ 未找到宝可梦「{name}」。"
        key, e = r
        types = "/".join(dex.type_label(t) for t in e.get("types", []))
        lines = [f"【{e.get('zh')} / {e.get('name')}】全国图鉴 No.{e.get('num')}"]
        if e.get("genus"):
            lines.append(f"分类: {e['genus']}")
        lines.append(f"属性: {types}")
        lines.append(
            f"身高 {e.get('heightm')}m  体重 {e.get('weightkg')}kg"
        )
        base = e.get("baseStats", {})
        lines.append(
            "种族值: "
            + " ".join(f"{dex.stat_label(s)} {base.get(s)}" for s in STAT_ORDER)
            + f"  合计 {sum(base.get(s, 0) for s in STAT_ORDER)}"
        )
        abs_ = []
        for slot, an in (e.get("abilities") or {}).items():
            ar = dex.resolve_ability(an)
            zh = ar[1]["zh"] if ar else an
            tag = "隐藏特性" if slot == "H" else "特性"
            abs_.append(f"{zh}({tag})")
        if abs_:
            lines.append("特性: " + "、".join(abs_))
        gr = e.get("genderRate", -1)
        if gr == -1:
            lines.append("性别: 无性别")
        elif gr == 0:
            lines.append("性别: 仅雄性")
        elif gr == 8:
            lines.append("性别: 仅雌性")
        elif 0 <= gr <= 8:
            female = gr * 12.5
            lines.append(f"性别: 雄性 {100 - female:g}% / 雌性 {female:g}%")
        eg = e.get("eggGroups") or []
        if eg:
            lines.append(
                f"蛋群: {'/'.join(EGG_ZH.get(g, g) for g in eg)}"
                f" | 孵化周期: {e.get('hatchCounter', 0)}"
            )
        evs = e.get("evs") or {}
        if evs:
            lines.append(
                "击败努力值: "
                + " ".join(f"{dex.stat_label(s)}+{v}" for s, v in evs.items())
            )
        cr = e.get("captureRate")
        if cr is not None:
            lines.append(
                f"捕获率: {cr}"
                + (" (传说)" if e.get("isLegendary") else "")
                + (" (幻之)" if e.get("isMythical") else "")
            )
        # 相克
        eff = []
        for atk_t in dex.type_zh:
            if atk_t == "Stellar":
                continue
            m = dex.type_multiplier(atk_t, e.get("types", []))
            if m > 1:
                eff.append(f"弱{dex.type_label(atk_t)}{m:g}x")
            elif m == 0:
                eff.append(f"免{dex.type_label(atk_t)}")
            elif m < 1:
                eff.append(f"抗{dex.type_label(atk_t)}")
        if eff:
            lines.append("防御相克: " + " ".join(eff))
        if e.get("prevo"):
            lines.append(
                f"进化前: {dex.species.get(e['prevo'], {}).get('zh', e['prevo'])}"
            )
        if e.get("evos"):
            lines.append(
                "可进化为: "
                + "、".join(dex.species.get(x, {}).get("zh", x) for x in e["evos"])
            )
        if e.get("requiredTeraType"):
            lines.append(f"固定太晶属性: {dex.type_label(e['requiredTeraType'])}")
        if e.get("requiredItem"):
            lines.append(f"需要道具: {e['requiredItem']} (key={key})")
        if e.get("flavor"):
            lines.append(f"图鉴说明: {e['flavor']}")
        if e.get("flavorEn"):
            lines.append(f"图鉴说明(EN): {e['flavorEn']}")
        return "\n".join(lines)

    async def poke_dex_move(self, event, name: str) -> str:
        """查询招式资料:属性、威力、命中、PP、优先度、效果说明。

        Args:
            name(string): 招式名称,支持中文/英文/标识,如 "十万伏特" / "thunderbolt"。
        """
        dex = get_dex()
        r = dex.resolve_move(name)
        if r is None:
            return f"❌ 未找到招式「{name}」。"
        key, m = r
        cat = {"Physical": "物理", "Special": "特殊", "Status": "变化"}.get(
            m.get("category"), m.get("category")
        )
        acc = m.get("accuracy")
        acc = "必中" if acc is True or acc is None else f"{acc}"
        lines = [
            f"【{m.get('zh')} / {m.get('name')}】",
            (
                f"属性 {dex.type_label(m.get('type'))} | 分类 {cat} | 威力 {m.get('basePower') or '—'}"
                f" | 命中 {acc} | PP {m.get('pp')} | 优先度 {m.get('priority') or 0:+d}"
            ),
        ]
        if m.get("target"):
            lines.append(f"目标: {m['target']}")
        if m.get("critRatio", 1) and m.get("critRatio", 1) > 1:
            lines.append("易击中要害。")
        if m.get("multihit"):
            lines.append(f"连续攻击: {m['multihit']}")
        if m.get("drain"):
            lines.append("吸收伤害回复自身。")
        if m.get("recoil"):
            lines.append("有反作用力伤害。")
        if m.get("status"):
            lines.append(f"造成异常状态: {STATUS_ZH.get(m['status'], m['status'])}")
        if m.get("boosts"):
            lines.append(f"能力变化: {m['boosts']}")
        if m.get("volatileStatus"):
            lines.append(f"附加状态: {m['volatileStatus']}")
        if m.get("desc"):
            lines.append(f"效果: {m['desc']}")
        lines.append(f"(key={key})")
        return "\n".join(lines)

    async def poke_dex_ability(self, event, name: str) -> str:
        """查询特性说明。

        Args:
            name(string): 特性名称,支持中文/英文/标识,如 "静电" / "static"。
        """
        dex = get_dex()
        r = dex.resolve_ability(name)
        if r is None:
            return f"❌ 未找到特性「{name}」。"
        key, a = r
        lines = [f"【{a.get('zh')} / {a.get('name')}】"]
        if a.get("desc"):
            lines.append(a["desc"])
        else:
            lines.append("(暂无详细说明;引擎已实现常见对战特性的核心效果)")
        lines.append(f"(key={key})")
        return "\n".join(lines)

    async def poke_dex_item(self, event, name: str) -> str:
        """查询对战道具说明。

        Args:
            name(string): 道具名称,支持中文/英文/标识,如 "吃剩的东西" / "leftovers"。
        """
        r = resolve_item(name)
        if r is None:
            return f"❌ 未找到道具「{name}」。(仅收录对战常用道具)"
        key, it = r
        return f"【{it['zh']}】{it['desc']}\n(key={key})"

    async def poke_type_matchup(self, event, attack_type: str, defender_types: str) -> str:
        """查询属性相克倍率(支持双属性)。

        Args:
            attack_type(string): 进攻属性,如 "电" / "Electric"。
            defender_types(string): 防守方属性,多个用逗号分隔,如 "水,飞行"。
        """
        dex = get_dex()
        atk = dex.resolve_type(attack_type)
        if not atk:
            return f"❌ 未知属性「{attack_type}」。"
        defs = [dex.resolve_type(t.strip()) for t in self._parse_moves(defender_types)]
        defs = [d for d in defs if d]
        if not defs:
            return "❌ 请提供至少一个防守属性。"
        mult = dex.type_multiplier(atk, defs)
        return (
            f"{dex.type_label(atk)} → {'/'.join(dex.type_label(d) for d in defs)}: "
            f"{mult:g}× ({dex.effectiveness_label(mult)})"
        )

    async def poke_learnset(self, event, species: str, level: int = 0, include_tm: bool = True) -> str:
        """查询宝可梦可学招式(等级招 / 招式学习器 / 教学)。

        Args:
            species(string): 宝可梦名称。
            level(int): Optional. 当前等级;>0 时只列出该等级已能学会的等级招。默认 0 = 全部。
            include_tm(bool): Optional. 是否包含招式学习器/教学招式。默认 true。
        """
        dex = get_dex()
        r = dex.resolve_species(species)
        if r is None:
            return f"❌ 未找到宝可梦「{species}」。"
        key, _ = r
        lv = int(level or 0)
        items = dex.learnable(
            key,
            level=lv if lv > 0 else 100,
            include_tm=bool(include_tm),
            include_tutor=bool(include_tm),
        )
        if not items:
            return f"「{dex.species[key]['zh']}」没有记录到可学招式。"
        code_zh = {
            "L": "升级",
            "M": "学习器",
            "T": "教学",
            "E": "蛋招",
            "S": "活动",
            "V": "虚拟主机",
        }

        def _fmt(methods: list) -> str:
            out = []
            for code in methods:
                s = str(code)
                if s.startswith("L") and s[1:].isdigit():
                    out.append(f"升级 Lv{s[1:]}")
                else:
                    out.append(code_zh.get(s[:1], s))
            return " / ".join(out)

        lv_items = [it for it in items if any(str(c).startswith("L") for c in it["methods"])]
        other = [it for it in items if not any(str(c).startswith("L") for c in it["methods"])]
        header = (
            f"【{dex.species[key]['zh']} 可学招式】(共 {len(items)} 个:"
            f"升级 {len(lv_items)} / 学习器·教学 {len(other)})"
        )
        lines = [
            header,
            "升级 = 等级提升自然学会;学习器 = 招式学习器/秘传机;教学 = 招式教学",
        ]
        if lv_items:
            lines.append("─ 升级招式 ─")
            for it in lv_items:
                m = dex.moves.get(it["move"]) or {}
                label = m.get("zh") or m.get("name") or it["move"]
                lines.append(f"- {label} ({_fmt(it['methods'])})")
        if other:
            lines.append("─ 学习器 / 教学 / 其他 ─")
            for it in other:
                m = dex.moves.get(it["move"]) or {}
                label = m.get("zh") or m.get("name") or it["move"]
                lines.append(f"- {label} ({_fmt(it['methods'])})")
        if len(lines) > 90:
            lines = lines[:90] + [f"…(其余 {len(lines) - 90} 行已省略,可用 level 参数过滤)"]
        return "\n".join(lines)

    # ──────────────────────────── 队伍工具 ────────────────────────────

    async def poke_team(self, event) -> str:
        """查看当前队伍与电脑(背包)中的宝可梦、等级、招式与对战状态。

        Args: 无。
        """
        data = self._poke_load(event)
        party = self._party_of(data)
        lines = [f"🎒 队伍({len(party)}/{MAX_PARTY})"]
        if not party:
            lines.append("(空;用 poke_add_pokemon 添加宝可梦)")
        for i, p in enumerate(party, 1):
            mon = Pokemon.from_dict(p)
            lines.append(self._fmt_mon(mon, i))
        box = data.get("box") or []
        if box:
            lines.append(f"📦 电脑存放:{len(box)} 只 — " + "、".join(
                get_dex().species.get(b.get("species", ""), {}).get("zh", b.get("species", ""))
                for b in box
            ))
        bag_line = self._fmt_bag(data)
        if bag_line:
            lines.append(bag_line)
        b = data.get("battle")
        if b:
            battle = battle_from_dict(b)
            lines.append("\n⚔️ 当前对战:\n" + battle.summary())
        return "\n".join(lines)

    @staticmethod
    def _fmt_bag(data: dict) -> str:
        bag = data.get("bag") or {}
        if not bag:
            return "🎒 背包: 空"
        by_kind: dict[str, list[str]] = {}
        for key, cnt in bag.items():
            entry = BAG_ITEMS.get(key)
            label = entry["zh"] if entry else key
            kind = (entry or {}).get("kind", "other")
            by_kind.setdefault(kind, []).append(f"{label}×{cnt}")
        order = KIND_ORDER + [k for k in by_kind if k not in KIND_ORDER]
        parts = []
        for kind in order:
            if kind in by_kind:
                parts.append(f"{KIND_ZH.get(kind, kind)}: " + "、".join(by_kind[kind]))
        return "🎒 背包: " + " | ".join(parts)

    async def poke_add_pokemon(
        self,
        event,
        species: str,
        level: int = 5,
        nickname: str = "",
        nature: str = "",
        ability: str = "",
        item: str = "",
        moves: str = "",
        tera_type: str = "",
        gender: str = "",
    ) -> str:
        """把一只宝可梦加入队伍(最多 6 只)。生成时会按图鉴自动补全能力值与招式。

        Args:
            species(string): 宝可梦名称(中/英/标识)。
            level(int): Optional. 等级 1-100,默认 5。
            nickname(string): Optional. 昵称。
            nature(string): Optional. 性格(如 "胆小" / "timid"),留空随机为认真。
            ability(string): Optional. 特性,留空用第一特性。
            item(string): Optional. 持有道具(仅收录对战常用道具)。
            moves(string): Optional. 招式列表,逗号分隔(最多 4 个);留空自动生成合理招式。
            tera_type(string): Optional. 太晶属性,留空用本系第一属性。
            gender(string): Optional. 性别 M/F(部分进化需要,如焰后蜥)。留空按图鉴比例随机。
        """
        dex = get_dex()
        data = self._poke_load(event)
        if self._battle_active(data):
            return "⚠️ 对战中不能调整队伍,请先用 poke_battle_end 结束对战(战斗中回复请用 item 道具名)。"
        party = self._party_of(data)
        if len(party) >= MAX_PARTY:
            return "❌ 队伍已满(6 只)。先 poke_remove_pokemon 或存入电脑。"
        try:
            mon = create_pokemon(
                species,
                level=int(level or 5),
                nickname=nickname,
                nature=nature,
                ability=ability,
                item=item,
                moves=self._parse_moves(moves) or None,
                tera_type=tera_type,
                gender=gender,
            )
        except ValueError as e:
            return f"❌ {e}"
        party.append(mon.to_dict())
        self._poke_save(event, data)
        return (
            f"✅ 已加入队伍:\n{self._fmt_mon(mon, len(party))}\n"
            f"(太晶属性 {dex.type_label(mon.tera_type)})"
        )

    async def poke_remove_pokemon(self, event, target: str) -> str:
        """把队伍中的宝可梦移到电脑存放(按序号或名称)。

        Args:
            target(string): 队伍序号(1 起)或名称,如 "2" / "皮卡丘"。
        """
        data = self._poke_load(event)
        if self._battle_active(data):
            return "⚠️ 对战中不能调整队伍,请先用 poke_battle_end 结束对战(战斗中回复请用 item 道具名)。"
        found = self._find_member(data, target)
        if not found:
            return f"❌ 队伍里找不到「{target}」。"
        idx, p = found
        del self._party_of(data)[idx]
        data.setdefault("box", []).append(p)
        self._poke_save(event, data)
        name = get_dex().species.get(p.get("species", ""), {}).get("zh", p.get("species"))
        return f"📦 已将 {name} 移到电脑存放。"

    async def poke_learn_move(self, event, target: str, move: str, replace: str = "") -> str:
        """让队伍中的宝可梦学会/替换招式(校验是否可学,最多 4 个)。

        Args:
            target(string): 队伍序号(1 起)或名称。
            move(string): 要学习的招式名(中/英/标识)。
            replace(string): Optional. 要遗忘的招式:可写序号(1-4)或招式名;留空且已满 4 个时会失败。
        """
        dex = get_dex()
        data = self._poke_load(event)
        if self._battle_active(data):
            return "⚠️ 对战中不能调整队伍,请先用 poke_battle_end 结束对战(战斗中回复请用 item 道具名)。"
        found = self._find_member(data, target)
        if not found:
            return f"❌ 队伍里找不到「{target}」。"
        _idx, p = found
        mr = dex.resolve_move(move)
        if mr is None:
            return f"❌ 未找到招式「{move}」。"
        move_key = mr[0]
        known = dex.learnset(p.get("species", ""))
        codes = known.get(move_key)
        if codes is None:
            return (
                f"❌ {p.get('species')} 无法学会「{mr[1].get('zh')}」"
                f"(不在其可学招式表内)。"
            )
        mon = Pokemon.from_dict(p)
        if move_key in mon.moves:
            return f"⚠️ {mon.display} 已经会「{mr[1].get('zh')}」了。"
        # 只靠升级才能学的招式必须够等级;招式机/教学/遗传/活动(非 L 开头)
        # 任意等级可学(与游戏一致),避免 Lv5 就学会 Lv56 的升级招。
        _parts = [c for c in codes.split(",") if c]
        _lv_need = min(
            (int(c[1:] or 0) for c in _parts if c.startswith("L")), default=0
        )
        if not any(not c.startswith("L") for c in _parts) and _lv_need > mon.level:
            return (
                f"⚠️ {mon.display} 需要在 Lv{_lv_need} 才能学会「{mr[1].get('zh')}」"
                f"(当前 Lv{mon.level})。"
            )
        if len(mon.moves) >= 4:
            ri = -1
            rq = str(replace or "").strip()
            if rq.isdigit():
                ri = int(rq) - 1
            elif rq:
                rr = dex.resolve_move(rq)
                if rr and rr[0] in mon.moves:
                    ri = mon.moves.index(rr[0])
            if ri < 0 or ri >= len(mon.moves):
                known_list = " ".join(
                    f"{i}.{(dex.moves.get(m) or {}).get('zh', m)}"
                    for i, m in enumerate(mon.moves, 1)
                )
                return (
                    f"⚠️ {mon.display} 已会 4 个招式({known_list})。"
                    f"请用 replace 指定要遗忘的招式(序号 1-4 或招式名)。"
                )
            forgotten = mon.moves[ri]
            mon.moves[ri] = move_key
            mon.pp.pop(forgotten, None)
            mon.pp[move_key] = int((dex.moves.get(move_key) or {}).get("pp", 10))
            result = (
                f"✅ {mon.display} 忘记了「{(dex.moves.get(forgotten) or {}).get('zh', forgotten)}」,"
                f"学会了「{mr[1].get('zh')}」!"
            )
        else:
            mon.moves.append(move_key)
            mon.pp[move_key] = int((dex.moves.get(move_key) or {}).get("pp", 10))
            result = f"✅ {mon.display} 学会了「{mr[1].get('zh')}」!"
        p.clear()
        p.update(mon.to_dict())
        self._poke_save(event, data)
        return result

    async def poke_edit_pokemon(
        self,
        event,
        target: str,
        level: int = 0,
        nature: str = "",
        ability: str = "",
        nickname: str = "",
        item: str = "",
        tera_type: str = "",
    ) -> str:
        """修改队伍中宝可梦的等级/性格/特性/昵称/道具/太晶属性(留空的项不变)。

        Args:
            target(string): 队伍序号(1 起)或名称。
            level(int): Optional. 新等级 1-100,0 表示不变。
            nature(string): Optional. 新性格。
            ability(string): Optional. 新特性。
            nickname(string): Optional. 新昵称。
            item(string): Optional. 新持有道具。
            tera_type(string): Optional. 新太晶属性。
        """
        dex = get_dex()
        data = self._poke_load(event)
        if self._battle_active(data):
            return "⚠️ 对战中不能调整队伍,请先用 poke_battle_end 结束对战(战斗中回复请用 item 道具名)。"
        found = self._find_member(data, target)
        if not found:
            return f"❌ 队伍里找不到「{target}」。"
        _idx, p = found
        mon = Pokemon.from_dict(p)
        changes = []
        if level and int(level) > 0:
            mon.level = max(1, min(100, int(level)))
            mon.stats = dex.compute_stats(
                mon.species, mon.level, mon.ivs, mon.evs, mon.nature
            )
            mon.max_hp = mon.stats["hp"]
            mon.exp = dex.exp_for_level(dex.growth_of(mon.species), mon.level)
            if mon.fainted:
                mon.cur_hp = 0  # 倒下的保持倒下,不因病改等级而“诈尸”
            else:
                mon.cur_hp = min(max(1, mon.cur_hp), mon.max_hp)
            changes.append(f"等级→{mon.level}")
        if nature:
            nk = dex.resolve_nature(nature)
            if nk:
                mon.nature = nk
                mon.stats = dex.compute_stats(
                    mon.species, mon.level, mon.ivs, mon.evs, mon.nature
                )
                mon.max_hp = mon.stats["hp"]
                mon.cur_hp = min(mon.cur_hp, mon.max_hp)
                changes.append(f"性格→{dex.natures[nk]['zh']}")
        if ability:
            ar = dex.resolve_ability(ability)
            if ar:
                mon.ability = ar[0]
                changes.append(f"特性→{ar[1]['zh']}")
        if nickname:
            mon.nickname = nickname.strip()
            changes.append(f"昵称→{nickname.strip()}")
        if item:
            ir = resolve_item(item)
            if ir is None:
                return f"❌ 未收录道具「{item}」。"
            mon.item = ir[0]
            changes.append(f"道具→{ir[1]['zh']}")
        if tera_type:
            tt = dex.resolve_type(tera_type)
            if not tt:
                return f"❌ 未知太晶属性「{tera_type}」。"
            mon.tera_type = tt
            mon.terastallized = False
            changes.append(f"太晶属性→{dex.type_label(tt)}")
        if not changes:
            return "⚠️ 未指定任何要修改的项。"
        p.clear()
        p.update(mon.to_dict())
        self._poke_save(event, data)
        return f"✅ {mon.display} 已更新: " + ", ".join(changes)

    async def poke_evolve(
        self,
        event,
        target: str,
        into: str = "",
        method: str = "",
        item: str = "",
        trade: bool = False,
        force: bool = False,
        daytime: str = "",
    ) -> str:
        """让宝可梦进化,支持等级/亲密度/招式/携带物/使用道具/交换等全部方式。

        Args:
            target(string): 队伍序号(1 起)或名称。
            into(string): Optional. 指定进化形态(中/英/标识);多分支(如伊布)时必须指定。
            method(string): Optional. 强制进化方式: level/friendship/move/hold/item/trade/extra。
            item(string): Optional. 用于使用道具/携带进化的道具名。
            trade(bool): Optional. 是否通过交换进化(trade 型)。默认 false。
            force(bool): Optional. 忽略未满足的条件强行进化(仅用于叙事需要)。
            daytime(string): Optional. day/night,用于昼夜限定进化。
        """
        dex = get_dex()
        data = self._poke_load(event)
        if self._battle_active(data):
            return "⚠️ 对战中不能调整队伍,请先用 poke_battle_end 结束对战(战斗中回复请用 item 道具名)。"
        found = self._find_member(data, target)
        if not found:
            return f"❌ 队伍里找不到「{target}」。"
        idx, p = found
        mon = Pokemon.from_dict(p)
        use_item = item
        if item:
            r = resolve_item(item) or resolve_bag_item(item)
            if r:
                use_item = r[0]
        held = use_item or mon.item
        opts = dex.evolution_options(
            mon.species,
            level=mon.level,
            moves=mon.moves,
            item=held,
            friendship=mon.friendship,
            gender=mon.gender,
            trade=trade,
            daytime=daytime or None,
            stats=mon.stats,
        )
        if not opts:
            return f"⚠️ {mon.display} 没有已知的进化形态。"
        kind_alias = {
            "level": "level",
            "friendship": "levelFriendship",
            "move": "levelMove",
            "hold": "levelHold",
            "item": "useItem",
            "trade": "trade",
            "extra": "levelExtra",
        }
        want_kind = kind_alias.get(method.strip().lower(), "")
        chosen = None
        if into:
            r = dex.resolve_species(into)
            if r is None:
                return f"❌ 未找到宝可梦「{into}」。"
            for o in opts:
                if o["target"] == r[0] or o["target"] == into:
                    chosen = o
                    break
            if chosen is None:
                lines = [
                    f"- {dex.species.get(o['target'], {}).get('zh', o['target'])}"
                    f"({o['kind']}{' / ' + o['reason'] if o['reason'] else ''})"
                    for o in opts
                ]
                return (
                    f"❌ {mon.display} 不能进化成「{into}」。可选:\n" + "\n".join(lines)
                )
        else:
            cands = [o for o in opts if o["met"] and (not want_kind or o["kind"] == want_kind)]
            if force and not cands:
                cands = [o for o in opts if not want_kind or o["kind"] == want_kind]
            if len(cands) == 1:
                chosen = cands[0]
            elif len(cands) > 1:
                lines = [
                    f"- {dex.species.get(o['target'], {}).get('zh', o['target'])}"
                    f"({o['kind']}{' / ' + o['reason'] if o['reason'] else ''})"
                    for o in cands
                ]
                return (
                    f"⚠️ {mon.display} 有多个进化分支,请用 into 指定:\n"
                    + "\n".join(lines)
                )
        if chosen is None:
            lines = [
                f"- {dex.species.get(o['target'], {}).get('zh', o['target'])}"
                f"({o['kind']}{' / ' + o['reason'] if o['reason'] else ''})"
                + ("✅" if o["met"] else "❌")
                for o in opts
            ]
            return (
                f"⚠️ {mon.display} 当前不满足进化条件(可用 force 或指定 into):\n"
                + "\n".join(lines)
            )
        if not chosen["met"] and not force:
            need: list[str] = []
            if chosen["kind"] == "levelFriendship":
                need.append(
                    f"亲密度需≥{dex._friendship_need(chosen['reason'])}"
                    f"(当前 {mon.friendship})"
                )
            if chosen["level"]:
                need.append(f"等级需≥{chosen['level']}(当前 {mon.level})")
            if chosen["item"]:
                need.append(f"需携带/使用 {chosen['item']}")
            if chosen["move"]:
                need.append(f"需学会「{chosen['move']}」")
            if chosen["reason"]:
                need.append(chosen["reason"])
            return (
                f"⚠️ {mon.display} 还不能进化成 "
                f"{dex.species.get(chosen['target'], {}).get('zh', chosen['target'])}:"
                + "、".join(need or ["条件未满足"])
            )
        msgs = self._do_evolve(mon, chosen["target"])
        p.clear()
        p.update(mon.to_dict())
        self._poke_save(event, data)
        return "\n".join(msgs) + "\n" + self._fmt_mon(mon, idx + 1)

    def _do_evolve(self, mon: Pokemon, new_key: str) -> list[str]:
        """执行进化:重算数值/特性/太晶属性,返回日志。"""
        dex = get_dex()
        old_zh = mon.entry.get("zh", mon.species)
        old_types = set(mon.entry.get("types") or [])
        mon.species = new_key
        new_entry = dex.species.get(new_key, {})
        mon.stats = dex.compute_stats(new_key, mon.level, mon.ivs, mon.evs, mon.nature)
        mon.max_hp = mon.stats["hp"]
        mon.cur_hp = mon.max_hp
        mon.fainted = False
        mon.faint_logged = False
        if not mon.tera_type or (mon.tera_type in old_types and mon.tera_type not in set(new_entry.get("types") or [])):
            mon.tera_type = new_entry.get("requiredTeraType") or (new_entry.get("types") or ["Normal"])[0]
        fixed = (new_entry.get("gender") or "").upper()[:1]
        if fixed in ("M", "F"):
            mon.gender = fixed
        allowed = {
            (dex.resolve_ability(a) or ("", ""))[0]
            for a in (new_entry.get("abilities") or {}).values()
        }
        if mon.ability not in allowed:
            default = (new_entry.get("abilities") or {}).get("0")
            ar = dex.resolve_ability(default) if default else None
            mon.ability = ar[0] if ar else ""
        msgs = [f"✨ {old_zh} 进化成了 {new_entry.get('zh', new_key)}!"]
        for mv in dex.level_up_moves(new_key, 0, mon.level):
            if mv in mon.moves:
                continue
            label = (dex.moves.get(mv) or {}).get("zh", mv)
            if len(mon.moves) < 4:
                mon.moves.append(mv)
                mon.pp[mv] = int((dex.moves.get(mv) or {}).get("pp", 10) or 10)
                msgs.append(f"   {mon.display} 学会了「{label}」!")
                break
        return msgs

    async def poke_heal_party(self, event) -> str:
        """回复全队 HP 与异常状态(宝可梦中心)。

        Args: 无。
        """
        data = self._poke_load(event)
        if self._battle_active(data):
            return "⚠️ 对战中不能调整队伍,请先用 poke_battle_end 结束对战(战斗中回复请用 item 道具名)。"
        party = self._party_of(data)
        if not party:
            return "⚠️ 队伍是空的。"
        for p in party:
            mon = Pokemon.from_dict(p)
            mon.full_heal()
            p.clear()
            p.update(mon.to_dict())
        self._poke_save(event, data)
        return f"💊 全队已回复({len(party)} 只)。"

    # ──────────────────────────── 训练 / 升级 / 多玩家 ────────────────────────────

    async def poke_trainer(self, event, name: str = "") -> str:
        """设置或查看自己的训练家名称(用于多玩家/PvP 显示)。

        Args:
            name(string): Optional. 训练家名称;留空则查看当前名称。
        """
        data = self._poke_load(event)
        if not name:
            return f"🗃️ 训练家: {data.get('trainer') or '(未设置)'} | 队伍 {len(self._party_of(data))} 只"
        data["trainer"] = str(name).strip()
        self._poke_save(event, data)
        return f"✅ 训练家名称已设为「{data['trainer']}」。"

    async def poke_trainers(self, event) -> str:
        """列出同群内所有玩家的训练家与队伍概况(用于多玩家/淘汰赛编排)。

        Args: 无。
        """
        gid = ""
        try:
            gid = str(event.get_group_id() or "")
        except Exception:
            gid = ""
        store = self._poke_store()
        rows = []
        prefix = f"group_{gid}_"
        for fn in store.list_scopes():
            d = store.load(fn)
            scope = str(d.get("scope") or fn)
            if gid and not scope.startswith(prefix):
                continue
            party = d.get("party") or []
            if not party:
                continue
            mons = [Pokemon.from_dict(p) for p in party]
            best = max((m.level for m in mons), default=0)
            uid = scope.removeprefix(prefix)
            rows.append((d.get("trainer") or f"玩家{uid}", len(party), best, uid))
        if not rows:
            return "本群还没有其他训练家。用 poke_trainer 设置名称后开始集结吧。"
        rows.sort(key=lambda r: -r[2])
        lines = ["🏆 本群训练家一览(按最高等级):"]
        for name, n, best, uid in rows:
            lines.append(f"- {name} [uid={uid}] | {n} 只 | 最高 Lv{best}")
        lines.append("(与某人对战:poke_battle_pvp opponent=\"<uid>\")")
        return "\n".join(lines)

    async def poke_train(self, event, target: str, sessions: int = 5, focus: str = "") -> str:
        """训练宝可梦:获得经验并可提升某项努力值(触发升级/学招/进化)。

        Args:
            target(string): 队伍序号(1 起)或名称。
            sessions(int): Optional. 训练量(1-50),越大获得经验/努力值越多,默认 5。
            focus(string): Optional. 重点训练的能力(攻击/防御/特攻/特防/速度/HP,或 atk/def/spa/spd/spe);留空则不加努力值。
        """
        dex = get_dex()
        data = self._poke_load(event)
        if self._battle_active(data):
            return "⚠️ 对战中不能调整队伍,请先用 poke_battle_end 结束对战(战斗中回复请用 item 道具名)。"
        found = self._find_member(data, target)
        if not found:
            return f"❌ 队伍里找不到「{target}」。"
        _idx, p = found
        mon = Pokemon.from_dict(p)
        n = max(1, min(50, int(sessions or 5)))
        msgs: list[str] = []
        stat = self._resolve_stat(focus)
        if focus and not stat:
            return f"❌ 未知的训练方向「{focus}」。"
        if stat:
            cur = dict(mon.evs or {})
            total = sum(int(v) for v in cur.values())
            cur_val = int(cur.get(stat, 0))
            gain = min(10 * n, 252 - cur_val, max(0, 510 - total))
            if gain > 0:
                cur[stat] = cur_val + gain
                mon.evs = cur
                mon.stats = dex.compute_stats(
                    mon.species, mon.level, mon.ivs, mon.evs, mon.nature
                )
                mon.max_hp = mon.stats["hp"]
                mon.cur_hp = min(mon.cur_hp, mon.max_hp)
                msgs.append(
                    f"💪 努力值 {dex.stat_label(stat)} +{gain}(现 {cur[stat]})"
                )
            else:
                msgs.append(f"💪 {dex.stat_label(stat)} 的努力值已满。")
        exp_gain = max(50, dex.base_exp(mon.species) * n * 2)
        mon.friendship = min(255, int(mon.friendship) + min(150, 3 * n))
        msgs += self._gain_exp(mon, exp_gain)
        msgs.append(f"❤️ 亲密度 {mon.friendship}/255")
        msgs.insert(0, f"🏋️ {mon.display} 完成了 {n} 轮训练(获得 {exp_gain} 经验)。")
        p.clear()
        p.update(mon.to_dict())
        self._poke_save(event, data)
        return "\n".join(msgs)

    @staticmethod
    def _resolve_stat(focus: str) -> str:
        if not focus:
            return ""
        dex = get_dex()
        f = str(focus).strip()
        low = f.lower()
        for s in STAT_ORDER:
            if low in (s, dex.stat_label(s).lower()):
                return s
        return _STAT_ALIAS.get(f, "")

    def _gain_exp(self, mon: Pokemon, amount: int) -> list[str]:
        """增加经验并根据成长曲线升级(自动学招/进化),返回日志。"""
        dex = get_dex()
        msgs: list[str] = []
        if amount:
            mon.exp = int(mon.exp) + int(amount)
        growth = dex.growth_of(mon.species)
        while mon.level < 100 and mon.exp >= dex.exp_for_level(growth, mon.level + 1):
            old_max = mon.max_hp
            old_level = mon.level
            mon.level += 1
            mon.stats = dex.compute_stats(
                mon.species, mon.level, mon.ivs, mon.evs, mon.nature
            )
            mon.max_hp = mon.stats["hp"]
            mon.cur_hp = min(
                mon.max_hp, mon.cur_hp + max(0, mon.max_hp - old_max)
            )
            msgs.append(f"⬆️ {mon.display} 升到了 Lv{mon.level}!")
            for mv in dex.level_up_moves(mon.species, old_level, mon.level):
                if mv in mon.moves:
                    continue
                label = (dex.moves.get(mv) or {}).get("zh", mv)
                if len(mon.moves) < 4:
                    mon.moves.append(mv)
                    mon.pp[mv] = int((dex.moves.get(mv) or {}).get("pp", 10) or 10)
                    msgs.append(f"   {mon.display} 学会了「{label}」!")
                else:
                    cur = " ".join(
                        f"{i}.{(dex.moves.get(m) or {}).get('zh', m)}"
                        for i, m in enumerate(mon.moves, 1)
                    )
                    msgs.append(
                        f"   {mon.display} 想学「{label}」,但招式已满:{cur}。"
                        f"请先询问玩家要遗忘哪个,再调 poke_learn_move(replace=序号或招式名)。"
                    )
            evos = dex.level_evolutions(
                mon.species,
                level=mon.level,
                moves=mon.moves,
                item=mon.item,
                friendship=mon.friendship,
                gender=mon.gender,
                stats=mon.stats,
            )
            mon.friendship = min(255, int(mon.friendship) + 5)
            if len(evos) == 1:
                msgs += ["   " + m for m in self._do_evolve(mon, evos[0]["target"])]
            elif len(evos) > 1:
                names = "、".join(
                    dex.species.get(o["target"], {}).get("zh", o["target"])
                    for o in evos
                )
                msgs.append(
                    f"   {mon.display} 可以进化成 {names},用 poke_evolve into=... 选择。"
                )
        return msgs

    def _award_battle_exp(self, battle: Battle) -> list[str]:
        """战斗胜利后,给存活的我方宝可梦分配经验与努力值(含升级日志)。"""
        dex = get_dex()
        if (
            not battle.finished
            or battle.winner != "player"
            or battle.escaped
            or battle.captured
        ):
            return []
        defeated = [p for p in battle.enemy.party if p.fainted]
        if not defeated:
            return []
        total = sum(
            dex.exp_yield(p.species, p.level, trainer=not battle.wild)
            for p in defeated
        )
        ev_gain: dict[str, int] = {}
        for p in defeated:
            for s, v in (p.entry.get("evs") or {}).items():
                ev_gain[s] = ev_gain.get(s, 0) + int(v)
        participants = [p for p in battle.player.party if not p.fainted]
        if not participants:
            participants = [p for p in battle.player.party if p.cur_hp > 0]
        if not participants:
            return []
        share = max(1, total // len(participants))
        msgs = [f"💰 获得经验总计 {total}(每只 {share})。"]
        for p in participants:
            msgs += self._gain_exp(p, share)
            if ev_gain:
                msgs += self._gain_evs(p, ev_gain)
            p.friendship = min(255, int(p.friendship) + 2)
        return msgs

    def _gain_evs(self, mon: Pokemon, evs: dict) -> list[str]:
        """增加努力值(单项 ≤252、总 ≤510),自动重算数值。"""
        dex = get_dex()
        cur = dict(mon.evs or {})
        total = sum(int(v) for v in cur.values())
        gained: list[str] = []
        for stat, amt in evs.items():
            if total >= 510:
                break
            cur_val = int(cur.get(stat, 0))
            add = min(int(amt), 252 - cur_val, 510 - total)
            if add <= 0:
                continue
            cur[stat] = cur_val + add
            total += add
            gained.append(f"{dex.stat_label(stat)}+{add}")
        if not gained:
            return []
        mon.evs = cur
        mon.stats = dex.compute_stats(mon.species, mon.level, mon.ivs, mon.evs, mon.nature)
        mon.max_hp = mon.stats["hp"]
        mon.cur_hp = min(mon.cur_hp, mon.max_hp)
        return [f"💪 {mon.display} 努力值 " + "、".join(gained)]

    async def poke_battle_pvp(self, event, opponent: str, weather: str = "", terrain: str = "") -> str:
        """与另一位玩家的真实队伍对战(PvP,双方 HP/PP/异常都会写回各自存档)。

        Args:
            opponent(string): 对手的 QQ/用户 id(可直接写数字,或 "@123" / "<@123>")。
            weather(string): Optional. 开场天气: sun/rain/sand/snow 或 晴天/下雨/沙暴/下雪。
            terrain(string): Optional. 开场场地: electric/grassy/misty/psychic。
        """
        data = self._poke_load(event)
        party = self._party_of(data)
        if not party:
            return "❌ 队伍是空的,先用 poke_add_pokemon 添加宝可梦。"
        player_party = [Pokemon.from_dict(p) for p in party]
        if not any(not m.fainted for m in player_party):
            return "❌ 我方全队已失去战斗能力,先用 poke_heal_party。"
        opp_scope = self._resolve_opp_scope(event, opponent)
        if not opp_scope:
            return "❌ 无法识别对手,请提供对手的 QQ/用户 id。"
        if opp_scope == self._poke_scope(event):
            return "❌ 不能和自己对战。"
        opp_data = self._poke_store().load(opp_scope)
        opp_party = opp_data.get("party") or []
        if not opp_party:
            return f"❌ 对手 {opp_data.get('trainer') or opp_scope} 还没有队伍。"
        enemy_party = [Pokemon.from_dict(p) for p in opp_party]
        if not any(not m.fainted for m in enemy_party):
            return "❌ 对手全队已失去战斗能力。"
        for m in enemy_party:
            m.tera_type = m.tera_type or m.original_types[0]
        battle = start_battle(
            player_party,
            enemy_party,
            weather=weather,
            terrain=terrain,
            seed=int(data.get("updated_at", 0) or 0) % 100000,
            wild=False,
            bag=data.get("bag") or {},
        )
        lines = battle.start()
        name = opp_data.get("trainer") or opp_scope
        bd = battle_to_dict(battle)
        bd["pvp_scope"] = opp_scope
        bd["pvp_name"] = name
        data["party"] = [m.to_dict() for m in player_party]
        data["battle"] = bd
        self._poke_save(event, data)
        self._sync_pvp(opp_scope, battle)
        return (
            f"⚔️ PvP 对战开始:你 vs {name}!\n"
            + "\n".join(lines)
            + "\n\n"
            + battle.summary()
        )

    def _resolve_opp_scope(self, event, opponent: str) -> str:
        raw = str(opponent or "").strip()
        if not raw:
            return ""
        if raw.startswith(("group_", "user_")):
            return raw
        m = re.search(r"\d{3,}", raw)
        if not m:
            return ""
        uid = m.group(0)
        try:
            gid = str(event.get_group_id() or "")
        except Exception:
            gid = ""
        return f"group_{gid}_{uid}" if gid else uid

    def _sync_pvp(self, opp_scope: str, battle: Battle) -> None:
        """把 PvP 中对手队伍的最新状态写回对手存档。"""
        store = self._poke_store()
        od = store.load(opp_scope)
        od["party"] = [m.to_dict() for m in battle.enemy.party]
        store.save(opp_scope, od)

    # ──────────────────────────── 背包 / 道具 ────────────────────────────

    async def poke_bag(self, event, item: str = "", count: int = 1) -> str:
        """查看或修改背包道具。不给 item 时查看当前背包。

        Args:
            item(string): Optional. 道具名称(中/英/标识,如 "精灵球" / "pokeball" / "potion");留空则只查看。
            count(int): Optional. 变更数量:正数获得,负数消耗/丢弃,默认 1。
        """
        data = self._poke_load(event)
        party = self._party_of(data)
        bag = data.setdefault("bag", {})
        if not item:
            return self._fmt_bag(data)
        r = resolve_bag_item(item)
        if r is None:
            return f"❌ 背包里没有收录「{item}」这种道具。"
        key, entry = r
        n = int(count)
        if n == 0:
            return "❌ count 不能为 0。"
        if n > 0:
            bag[key] = int(bag.get(key, 0)) + n
        else:
            have = int(bag.get(key, 0))
            if have <= 0:
                return f"❌ 背包里没有 {entry['zh']}。"
            bag[key] = have + n
            if bag[key] <= 0:
                del bag[key]
        self._poke_save(event, data)
        return f"✅ 背包已更新:\n{self._fmt_bag(data)}" + (
            f"\n(队伍 {len(party)} 只)" if party else ""
        )

    async def poke_use_item(self, event, target: str, item: str) -> str:
        """对队伍中的宝可梦使用道具(战斗外):伤药 / 状态回复 / 复活 / PP 回复 /进化石 / 神奇糖果等。

        Args:
            target(string): 队伍序号(1 起)或名称。
            item(string): 道具名称(中/英/标识,如 "伤药" / "potion" / "火之石")。
        """
        dex = get_dex()
        data = self._poke_load(event)
        if data.get("battle") and not (data["battle"] or {}).get("finished"):
            return "⚠️ 对战中请用 poke_battle_turn 的 \"item 道具名\" 行动。"
        found = self._find_member(data, target)
        if not found:
            return f"❌ 队伍里找不到「{target}」。"
        _idx, p = found
        r = resolve_bag_item(item)
        if r is None:
            return f"❌ 未收录道具「{item}」。"
        key, entry = r
        bag = data.setdefault("bag", {})
        if int(bag.get(key, 0)) <= 0:
            return f"❌ 背包里没有 {entry['zh']}。"
        mon = Pokemon.from_dict(p)
        eff = entry.get("effect") or {}
        msgs: list[str] = []
        consumed = True

        # ── 使用道具进化(进化石 / 苹果 / 铠甲等)──
        if eff.get("evolve_stone") or eff.get("evolve_item"):
            opts = dex.use_item_evolutions(mon.species, key, gender=mon.gender)
            if not opts:
                return f"⚠️ {mon.display} 对 {entry['zh']} 没有反应。"
            msgs += self._do_evolve(mon, opts[0]["target"])
        # ── 神奇糖果 ──
        elif eff.get("level_up"):
            if mon.level >= 100:
                return f"⚠️ {mon.display} 已经是 100 级。"
            mon.level = min(100, mon.level + int(eff["level_up"]))
            mon.stats = dex.compute_stats(mon.species, mon.level, mon.ivs, mon.evs, mon.nature)
            mon.max_hp = mon.stats["hp"]
            mon.exp = dex.exp_for_level(dex.growth_of(mon.species), mon.level)
            mon.full_heal()
            msgs.append(f"{mon.display} 升到了 Lv{mon.level}!")
        # ── PP 提升剂(未实装)──
        elif eff.get("pp_up"):
            return f"⚠️ 暂不支持使用 {entry['zh']}(PP 上限提升尚未实装)。"
        # ── 特性胶囊 / 膏药 ──
        elif eff.get("ability_switch") or eff.get("ability_patch"):
            slots = dex.species.get(mon.species, {}).get("abilities") or {}
            want_hidden = bool(eff.get("ability_patch"))
            cands = []
            for slot, an in slots.items():
                is_hidden = slot == "H"
                if is_hidden != want_hidden:
                    continue
                ar = dex.resolve_ability(an)
                if ar and ar[0] != mon.ability:
                    cands.append(ar[0])
            if not cands:
                return f"⚠️ {mon.display} 没有可切换的特性。"
            mon.ability = cands[0]
            msgs.append(f"{mon.display} 的特性变成了 {mon.ability_name}!")
        # ── 回复 / 状态 / 复活 / PP ──
        else:
            if mon.fainted and not (eff.get("revive") or eff.get("revive_full")):
                return f"⚠️ {mon.display} 已失去战斗能力,需要用复活类道具。"
            before = (mon.cur_hp, mon.status, dict(mon.pp), mon.level)
            battle = Battle(
                player=Side(name="player", party=[mon]),
                enemy=Side(name="enemy", party=[]),
            )
            battle._apply_item_effect(mon, eff)
            after = (mon.cur_hp, mon.status, dict(mon.pp), mon.level)
            if before == after:
                consumed = False
                msgs.append(f"{mon.display} 使用了 {entry['zh']},但似乎没有效果……")
            else:
                msgs.append(f"{mon.display} 使用了 {entry['zh']}。")
                msgs.extend(battle.log)

        if not msgs:
            return f"⚠️ 无法对 {mon.display} 使用 {entry['zh']}。"
        p.clear()
        p.update(mon.to_dict())
        if consumed:
            bag[key] = int(bag.get(key, 0)) - 1
            if bag[key] <= 0:
                del bag[key]
        self._poke_save(event, data)
        return "\n".join(msgs)

    @staticmethod
    def _mon_sig(d: dict) -> tuple:
        return (
            d.get("species"),
            d.get("nickname") or "",
            d.get("level"),
            d.get("nature") or "",
        )

    def _sync_battle_party(self, data: dict, battle) -> None:
        """把对战中的 HP/PP/异常等同步回队伍存档(按成员匹配,保留增删)。"""
        party = self._party_of(data)
        snap = {self._mon_sig(m.to_dict()): m.to_dict() for m in battle.player.party}
        used: set = set()
        for p in party:
            sig = self._mon_sig(p)
            if sig in snap and sig not in used:
                used.add(sig)
                p.update(snap[sig])
        # 队伍里找不到的(极端情况)按顺序补齐,避免状态丢失
        missing = [d for s, d in snap.items() if s not in used]
        for d in missing:
            if len(party) < MAX_PARTY:
                party.append(d)

    @staticmethod
    def _battle_active(data: dict) -> bool:
        b = data.get("battle")
        return bool(b) and not (b or {}).get("finished")

    # ──────────────────────────── 对战工具 ────────────────────────────

    async def poke_wild_encounter(
        self,
        event,
        area: str = "",
        region: str = "",
        level: int = 0,
        gen: int = 0,
        allow_rare: bool = False,
        version_group: str = "",
        environment: str = "",
    ) -> str:
        """根据地点抽取一只野生宝可梦并直接开战(推荐用它代替自编对手)。

        若地点能匹配到真实地区(如 "常青森林"/"Eterna Forest"/"真新镇"),
        则按该地真野外分布(物种/等级/出现率/遭遇方式)抽取;
        否则回退到按生态属性加权抽取。传说/幻兽等默认不出现。

        Args:
            area(string): Optional. 地点或生态关键词,如 "常青森林"、"201号道路"、"水面"、"夜晚的洞窟"、"废弃发电厂"。
            region(string): Optional. 地区:关都/城都/丰缘/神奥/合众/卡洛斯/阿罗拉/伽勒尔/帕底亚。
            level(int): Optional. 野生等级;0(默认)按该地点真实等级范围(无地点匹配时按玩家队首 ±3)。
            gen(int): Optional. 限定世代 1-9(无地点匹配时生效)。
            allow_rare(bool): Optional. 是否允许传说/幻兽出现,默认 false。
            version_group(string): Optional. 指定作品版本(如 "platinum"、"black-2"、"ultra-sun");默认取该地点最新版本。
            environment(string): Optional. 遭遇环境:land/water/fish/all;默认从 area 文本推断。
        """
        dex = get_dex()
        data = self._poke_load(event)
        party = self._party_of(data)
        if not party:
            return "❌ 队伍是空的,无法进行野生遭遇。先 poke_add_pokemon。"
        rng = random.Random()
        loc_key = dex.find_location(area, region) if area else ""
        enc = None
        note = ""
        if loc_key:
            area_zh = (dex.locations.get(loc_key) or {}).get("zh") or loc_key
            env0 = environment or detect_environment(area)
            for env in list(dict.fromkeys([env0, "land", "water", "fish", "all"])):
                enc = roll_location_encounter(
                    dex,
                    loc_key,
                    version_group=version_group,
                    environment=env,
                    level=int(level or 0),
                    include_special=bool(allow_rare),
                    rng=rng,
                )
                if enc:
                    break
            if enc is None:  # 该地仅有点定/赠予类遭遇
                enc = roll_location_encounter(
                    dex,
                    loc_key,
                    version_group=version_group,
                    environment="all",
                    level=int(level or 0),
                    include_special=True,
                    rng=rng,
                )
                if enc:
                    note = "(定点/特殊遭遇)"
        if enc is None:
            enc = roll_encounter(
                dex,
                area=area or "",
                region=region or "",
                gen=int(gen or 0),
                allow_rare=bool(allow_rare),
                rng=rng,
            )
            area_zh = ""
        if enc is None:
            return (
                f"❌ 在「{area or '此地'}」没有找到合适的野生宝可梦"
                "(可换地点或生态关键词,或查 poke_dex_location 看该地分布)。"
            )
        if loc_key:
            lvl = int(enc["level"])
            labels = "、".join(dex.location_methods.get(m, m) for m in enc["methods"][:2])
            head = (
                f"🌿 野生的 {enc['zh']} 出现了!(Lv{lvl} · "
                + " / ".join(dex.type_label(t) for t in enc["types"])
                + f" · 稀有度:{enc['rarity']})\n"
                f"📍 {area_zh} · {labels}{note} · 该地等级 {enc['min']}-{enc['max']}"
            )
        else:
            lvl = roll_level(dex, party, level=int(level or 0), rng=rng)
            head = (
                f"🌿 野生的 {enc['zh']} 出现了!(Lv{lvl} · "
                + " / ".join(dex.type_label(t) for t in enc["types"])
                + f" · 稀有度:{enc['rarity']})"
            )
        out = await self.poke_battle_start(event, enemy=f"{enc['zh']}|{lvl}", wild=True)
        entry = dex.species.get(enc["species"]) or {}
        flav = entry.get("flavor") or entry.get("flavorEn") or ""
        if flav:
            head += f"\n📖 {flav}"
        return head + "\n\n" + out

    async def poke_dex_location(
        self,
        event,
        name: str = "",
        region: str = "",
        version_group: str = "",
        include_special: bool = False,
    ) -> str:
        """查询地点野外分布:某地会出现哪些宝可梦、等级与出现率。

        Args:
            name(string): Optional. 地点名(中/英/标识,如 "常青森林"/"Eterna Forest"/"eterna-forest");留空则列出该地区的地点。
            region(string): Optional. 地区:关都/城都/丰缘/神奥/合众/卡洛斯/阿罗拉/伽勒尔/帕底亚。
            version_group(string): Optional. 作品版本(如 "platinum");默认取该地点最新版本。
            include_special(bool): Optional. 是否包含定点/赠予等特殊遭遇,默认 false。
        """
        dex = get_dex()
        region = dex.resolve_region(region) or region
        if not name:
            if not region:
                rows = [
                    f"- {v.get('zh', k)}({k}): {sum(1 for x in dex.locations.values() if x.get('region') == k)} 处"
                    for k, v in dex.location_regions.items()
                    if k
                ]
                return (
                    "📍 可用地区(传 region=… 查看地点,或直接传 name=地点):\n"
                    + "\n".join(rows)
                )
            keys = dex.list_locations(region)
            if not keys:
                return f"❌ 未收录地区「{region}」的地点(可用:关都/城都/丰缘/神奥/合众/卡洛斯/阿罗拉/伽勒尔/帕底亚)。"
            zh = dex.location_regions.get(region, {}).get("zh", region)
            lines = [f"📍 {zh} 共 {len(keys)} 处地点:"]
            lines += [f"- {dex.locations[k].get('zh') or k}" for k in keys]
            if len(lines) > 100:
                lines = lines[:100] + [f"…(其余 {len(keys) - 99} 处已省略)"]
            return "\n".join(lines)

        key = dex.find_location(name, region)
        if not key:
            return f"❌ 未收录地点「{name}」。(可先用 poke_dex_location(region=…) 看地点列表)"
        v = dex.locations[key]
        vg = version_group or dex.location_default_vg(key)
        label = (dex.version_groups.get(vg) or {}).get("label", vg)
        pools = dex.location_pools(key, vg, include_special=include_special)
        if not pools:
            return f"⚠️ {v.get('zh', key)} 在当前版本({label})没有自然野外分布。"
        by_method: dict[str, list[dict]] = {}
        for p in pools:
            by_method.setdefault(p["method"], []).append(p)
        lines = [
            f"📍 {v.get('zh') or key}({v.get('name', key)})"
            f" · 地区:{dex.location_regions.get(v.get('region', ''), {}).get('zh', v.get('region'))}"
            f" · 版本:{label}"
            + (" · 别名:" + "、".join(v.get("aliases") or []) if v.get("aliases") else "")
        ]
        order = [
            "walk",
            "grass-spots",
            "dark-grass",
            "cave-spots",
            "surf",
            "old-rod",
            "good-rod",
            "super-rod",
            "rock-smash",
            "overworld",
        ]
        keys_sorted = sorted(by_method, key=lambda m: (order.index(m) if m in order else 99, m))
        for m in keys_sorted:
            rows = sorted(by_method[m], key=lambda x: -x["chance"])
            mlabel = dex.location_methods.get(m, m)
            parts = []
            for p in rows[:16]:
                sp = dex.species.get(p["species"]) or {}
                nm = sp.get("zh") or sp.get("name") or p["species"]
                parts.append(f"{nm} Lv{p['min']}-{p['max']}({p['chance']}%)")
            more = f" …+{len(rows) - 16}" if len(rows) > 16 else ""
            lines.append(f"─ {mlabel} ─\n  " + "、".join(parts) + more)
        return "\n".join(lines)

    async def poke_trainer_battle(
        self,
        event,
        trainer: str = "",
        members: str = "",
        theme: str = "",
        location: str = "",
        ace: str = "",
        level: int = 0,
        size: int = 0,
        difficulty: str = "",
        region: str = "",
        gen: int = 0,
        weather: str = "",
        terrain: str = "",
        allow_rare: bool = False,
    ) -> str:
        """与 NPC/训练家对战。未指定队伍时,按玩家当前强度自动生成 NPC 队伍。

        等级默认跟随玩家队首等级,并按训练家级别(短裤小子→冠军)与难度修正;
        队伍规模与物种强度也随之变化,使 NPC 贴合当前剧情进度。
        若玩家/剧情已确定该 NPC 的宝可梦,请用 members 明确指定。

        Args:
            trainer(string): Optional. 训练家称呼,如 "岩石道馆馆主小刚"、"精英训练家"、"冠军"、"宿敌小茂"。影响队伍规模/强度/属性主题。
            members(string): Optional. 明确指定该 NPC 的宝可梦(同 poke_battle_start 的 enemy 语法:"名称|等级|招式|道具" 分号分隔)。一旦指定则不再自动生成。
            theme(string): Optional. 属性主题(如 "岩石"、"水/冰");不填时尝试从 trainer 名称解析(如"岩石道馆")。
            location(string): Optional. 地点(如 "常青森林"/"岩山隧道");给出时队伍物种限定为该地真实出现过的宝可梦,更贴近原作。
            ace(string): Optional. 指定王牌宝可梦(如 "大岩蛇"),放在最后一只。
            level(int): Optional. 强制 NPC 等级(剧情需要固定强度时用);0 表示按玩家队首等级自动缩放。
            size(int): Optional. 强制队伍规模 1-6;0 表示按训练家级别决定。
            difficulty(string): Optional. 难度修正:easy/normal/hard 或 简单/普通/困难。
            region(string): Optional. 地区,限定 NPC 宝可梦的全国图鉴范围:关都/城都/丰缘/神奥/合众/卡洛斯/阿罗拉/伽勒尔/帕底亚。
            gen(int): Optional. 限定世代 1-9(与 region 二选一即可)。
            weather(string): Optional. 开场天气: sun/rain/sand/snow。
            terrain(string): Optional. 开场场地: electric/grassy/misty/psychic。
            allow_rare(bool): Optional. 是否允许传说/幻兽(冠军/四天王剧情可用),默认 false。
        """
        dex = get_dex()
        data = self._poke_load(event)
        party = self._party_of(data)
        if not party:
            return "❌ 队伍是空的,无法进行训练家对战。先 poke_add_pokemon。"
        if members:
            out = await self.poke_battle_start(
                event,
                enemy=members,
                trainer=True,
                weather=weather,
                terrain=terrain,
            )
            return f"🎽 {trainer or '训练家'} 的队伍(剧情指定):\n" + out

        loc_key = dex.find_location(location, region) if location else ""
        gen = generate_team(
            dex,
            party=party,
            trainer=trainer,
            theme=theme,
            level=int(level or 0),
            size=int(size or 0),
            difficulty=difficulty,
            location=loc_key,
            ace=ace,
            region=region,
            gen=int(gen or 0),
            allow_rare=bool(allow_rare),
            rng=random.Random(),
        )
        team = gen["team"]
        if not team:
            return (
                f"❌ 没有找到适合「{trainer or '该训练家'}」的宝可梦"
                "(可换个 theme 或 allow_rare=true)。"
            )
        enemy_str = team_to_enemy_string(team, item_label)
        out = await self.poke_battle_start(
            event,
            enemy=enemy_str,
            trainer=True,
            weather=weather,
            terrain=terrain,
        )
        theme_txt = " / ".join(dex.type_label(t) for t in gen["theme"]) if gen["theme"] else "均衡"
        loc_txt = ""
        if loc_key:
            loc_name = (dex.locations.get(loc_key) or {}).get("zh") or loc_key
            loc_txt = f" · 地点:{loc_name}"
        header = (
            f"🎽 {trainer or '训练家'} 派出了 {len(team)} 只宝可梦!"
            f"(主题:{theme_txt}{loc_txt} · 基准 Lv{gen['level']})"
        )
        lines = [header]
        for i, m in enumerate(team, 1):
            tag = " · 王牌" if m["is_ace"] else ""
            it = f" · 携带 {item_label(m['item'])}" if m["item"] else ""
            lines.append(f"  {i}. {m['zh']} Lv{m['level']}{it}{tag}")
        return "\n".join(lines) + "\n\n" + out

    async def poke_battle_start(
        self,
        event,
        enemy: str,
        enemy_level: int = 50,
        enemy_moves: str = "",
        enemy_ability: str = "",
        enemy_item: str = "",
        weather: str = "",
        terrain: str = "",
        wild: bool = True,
        trainer: bool = False,
    ) -> str:
        """开始一场宝可梦对战(单打)。对手以分号分隔多只组成训练家队伍。

        Args:
            enemy(string): 对手。单只写名称;多只用分号分隔,每项可写 "名称|等级|招式1+招式2|道具|特性"。例如 "皮卡丘|50|十万伏特+电光一闪|讲究眼镜;喷火龙|52"。
            enemy_level(int): Optional. 对手默认等级(每项未写等级时使用),默认 50。
            enemy_moves(string): Optional. 对手默认招式(逗号分隔),每项未写招式时使用。
            enemy_ability(string): Optional. 对手特性(仅单只时生效)。
            enemy_item(string): Optional. 对手道具(仅单只时生效)。
            weather(string): Optional. 开场天气: sun/rain/sand/snow 或 晴天/下雨/沙暴/下雪。
            terrain(string): Optional. 开场场地: electric/grassy/misty/psychic。
            wild(bool): Optional. 是否野生战。只有野生战才能投球捕获/逃跑,默认 true。
            trainer(bool): Optional. 是否为训练家战(道馆/联盟/NPC/对手)。为 true 时强制不可捕获、不可逃跑;多只对手时自动视为训练家战。与训练家对战务必设为 true。
        """
        data = self._poke_load(event)
        party = self._party_of(data)
        if not party:
            return "❌ 队伍是空的,先用 poke_add_pokemon 添加宝可梦。"
        player_party = [Pokemon.from_dict(p) for p in party]
        if not any(not m.fainted for m in player_party):
            return "❌ 全队已失去战斗能力,先用 poke_heal_party 回复。"
        # 对手解析
        enemy_party: list[Pokemon] = []
        entries = [e.strip() for e in str(enemy).replace("；", ";").split(";") if e.strip()]
        for i, ent in enumerate(entries):
            parts = [x.strip() for x in ent.split("|")]
            sp = parts[0]
            if not sp:
                continue
            lv = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else int(enemy_level or 50)
            mv = self._parse_moves(parts[2].replace("+", ",")) if len(parts) > 2 else []
            ent_item = ""
            if len(parts) > 3 and parts[3]:
                _ir = resolve_item(parts[3])
                ent_item = _ir[0] if _ir else ""
            ent_ab = parts[4] if len(parts) > 4 else ""
            if not mv:
                mv = self._parse_moves(enemy_moves)
            single = len(entries) == 1
            try:
                enemy_party.append(
                    create_pokemon(
                        sp,
                        level=lv,
                        ability=ent_ab or (enemy_ability if single else ""),
                        item=ent_item or (enemy_item if single else ""),
                        moves=mv or None,
                    )
                )
            except ValueError as e:
                return f"❌ {e}"
        if not enemy_party:
            return "❌ 请提供至少一只对手宝可梦。"
        for m in enemy_party:
            m.tera_type = m.original_types[0]
        battle = start_battle(
            player_party,
            enemy_party,
            weather=weather,
            terrain=terrain,
            seed=int(data.get("updated_at", 0) or 0) % 100000 + len(entries),
            wild=bool(wild) and not trainer and len(enemy_party) == 1,
            bag=data.get("bag") or {},
        )
        lines = battle.start()
        data["party"] = [m.to_dict() for m in player_party]
        data["battle"] = battle_to_dict(battle)
        self._poke_save(event, data)
        kind = "野生" if battle.wild else "训练家"
        return (
            f"⚔️ {kind}对战开始!\n"
            + "\n".join(lines)
            + "\n\n"
            + battle.summary()
        )

    async def poke_battle_status(self, event) -> str:
        """查看当前对战状态与最近一次行动日志。

        Args: 无。
        """
        data = self._poke_load(event)
        b = data.get("battle")
        if not b:
            return "当前没有进行中的对战。"
        battle = battle_from_dict(b)
        out = battle.summary()
        if b.get("pvp_name"):
            out = f"【PvP vs {b['pvp_name']}】\n" + out
        if battle.log:
            out += "\n\n最近日志:\n" + "\n".join(battle.log[-12:])
        return out

    async def poke_battle_turn(self, event, action: str, enemy_action: str = "auto") -> str:
        """推进一回合对战。会返回结算日志与最新状态。

        Args:
            action(string): 我方行动。支持 "move 招式名"(出招)、"tera move 招式名"(太晶化后出招)、"switch 序号"(换人)、"item 道具名"(战斗中使用背包道具)、"catch 精灵球名"(野生战投球)、"run"(野生战逃走)、"forfeit"(认输);也可只写招式名。
            enemy_action(string): Optional. 对手行动,默认 "auto" 由 AI 决定;也可写成与 action 相同的格式。
        """
        data = self._poke_load(event)
        b = data.get("battle")
        if not b:
            return "❌ 当前没有进行中的对战,请先 poke_battle_start。"
        battle = battle_from_dict(b)
        already_finished = battle.finished
        pa = self._parse_battle_action(action, battle, side="player")
        if isinstance(pa, str):
            return pa
        if str(enemy_action).strip().lower() in ("auto", ""):
            ea = "auto"
        else:
            ea = self._parse_battle_action(enemy_action, battle, side="enemy")
            if isinstance(ea, str):
                return ea
        lines = battle.step(pa, ea)
        pvp_scope = b.get("pvp_scope")
        exp_msgs = [] if already_finished else self._award_battle_exp(battle)
        bd = battle_to_dict(battle)
        if pvp_scope:
            bd["pvp_scope"] = pvp_scope
            bd["pvp_name"] = b.get("pvp_name")
        data["battle"] = bd
        # 同步我方队伍状态(HP/PP/异常/能力)——按成员匹配就地更新,
        # 不用 battle 快照整体覆盖,以免抹掉战斗期间对队伍的其他修改
        self._sync_battle_party(data, battle)
        data["bag"] = dict(battle.bag)
        if pvp_scope:
            self._sync_pvp(pvp_scope, battle)
        # 捕获成功:把宝可梦加入队伍(满则进电脑)
        extra = ""
        if battle.captured:
            caught = Pokemon.from_dict(battle.captured)
            party = self._party_of(data)
            if len(party) < MAX_PARTY:
                party.append(caught.to_dict())
                extra = f"\n📥 {caught.display} 加入了队伍!"
            else:
                data.setdefault("box", []).append(caught.to_dict())
                extra = f"\n📦 队伍已满,{caught.display} 被送进了电脑。"
            battle.captured = None
            data["battle"] = battle_to_dict(battle)
            if pvp_scope:
                data["battle"]["pvp_scope"] = pvp_scope
                data["battle"]["pvp_name"] = b.get("pvp_name")
        self._poke_save(event, data)
        out = "\n".join(lines)
        if exp_msgs:
            out += "\n" + "\n".join(exp_msgs)
        if battle.finished:
            if battle.escaped:
                out += "\n\n🏁 成功逃走了。"
            elif battle.winner == "player":
                out += "\n\n🏁 胜利!对方全灭。"
            else:
                out += "\n\n🏁 败北……我方全灭。"
        out += extra
        out += "\n\n" + battle.summary()
        return out

    async def poke_battle_end(self, event) -> str:
        """结束并清除当前对战(不影响队伍状态)。

        Args: 无。
        """
        data = self._poke_load(event)
        b = data.get("battle")
        if not b:
            return "当前没有进行中的对战。"
        if b.get("pvp_scope"):
            self._sync_pvp(
                b["pvp_scope"],
                battle_from_dict(b),
            )
        data["battle"] = None
        self._poke_save(event, data)
        return "✅ 已结束对战。"

    # ── 对战行动解析 ──

    def _parse_battle_action(self, raw: str, battle: Battle, side: str):
        s = str(raw or "").strip()
        if not s:
            return "❌ 行动为空。请用 \"move 招式名\" / \"switch 序号\" / \"forfeit\"。"
        low = s.lower()
        if low in ("forfeit", "认输", "投降", "放弃"):
            return {"type": "forfeit"}
        if low in ("run", "escape", "逃走", "逃跑", "逃"):
            return {"type": "run"}
        # 使用道具
        for kw in ("item ", "use ", "道具 ", "使用 "):
            if low.startswith(kw):
                name = s[len(kw):].strip()
                r = resolve_bag_item(name)
                if r is None:
                    return f"❌ 未收录道具「{name}」。"
                if r[1].get("kind") == "ball":
                    return f"❌ {r[1]['zh']}是精灵球,请用 \"catch {r[1]['zh']}\"。"
                return {"type": "item", "item": r[0]}
        # 投球捕获
        for kw in ("catch", "ball", "throw", "投球", "捕获", "捕捉"):
            if low.startswith(kw):
                name = s[len(kw):].strip()
                if not name:
                    key = "poke-ball"
                else:
                    r = resolve_bag_item(name)
                    if r is None:
                        return f"❌ 未收录精灵球「{name}」。"
                    if r[1].get("kind") != "ball":
                        return f"❌ {r[1]['zh']}不是精灵球。"
                    key = r[0]
                return {"type": "catch", "item": key}
        tera = False
        if low.startswith("tera ") or s.startswith("太晶"):
            tera = True
            s = s.split(" ", 1)[1] if " " in s else s[2:]
            low = s.lower()
        if low.startswith(("switch", "换人", "替换")):
            rest = s.split(" ", 1)[1] if " " in s else ""
            rest = rest.strip()
            target_side = battle.enemy if side == "enemy" else battle.player
            if rest.isdigit():
                idx = int(rest) - 1
                if not 0 <= idx < len(target_side.party):
                    return (
                        f"❌ 换人序号 {rest} 越界"
                        f"({('敌方' if side == 'enemy' else '我方')}队伍共 "
                        f"{len(target_side.party)} 只)。"
                    )
                if idx == target_side.active:
                    return f"❌ 序号 {rest} 已经在场上。"
                if target_side.party[idx].fainted:
                    return f"❌ 序号 {rest} 已失去战斗能力,无法上场。"
            else:
                data = {"party": [m.to_dict() for m in target_side.party]}
                found = self._find_member(data, rest)
                if not found:
                    return f"❌ 找不到要换上场的宝可梦「{rest}」。"
                idx = found[0]
            return {"type": "switch", "index": idx}
        if low.startswith("move "):
            s = s.split(" ", 1)[1].strip()
        dex = get_dex()
        r = dex.resolve_move(s)
        if r is None:
            return f"❌ 未找到招式「{s}」。"
        return {"type": "move", "move": r[0], "tera": tera}

    # ── 数据统计辅助(给 WebUI 用)──

    def pokemon_team_size(self, scope: str) -> int:
        try:
            return len(self._poke_store().load(scope).get("party") or [])
        except Exception:
            return 0
