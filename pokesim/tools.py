"""`poke_*` 工具:Mixin 给主插件,供 LLM 在「宝可梦模式」中查询数据与推演对战。

所有工具与 `rpg_*` 一样,通过 `_build_my_tool_set` 自动收集注册:
只需 `poke_` 前缀 + Google 风格 docstring。

状态存在独立文件 `<data_dir>/pokemon/<scope>.json`(见 `store.PokeStore`),
不写回 sim session,避免与 `_generate` 末尾的整体落库互相覆盖。
"""

from __future__ import annotations

import re

from .dex import STAT_ORDER, get_dex
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
    bag_item_label,
    item_label,
    resolve_bag_item,
    resolve_item,
)
from .store import PokeStore

MAX_PARTY = 6

EGG_ZH = {
    "monster": "怪兽",
    "water1": "水中1",
    "bug": "虫",
    "flying": "飞行",
    "field": "陆上",
    "fairy": "妖精",
    "grass": "植物",
    "human-like": "人形",
    "water3": "水中3",
    "mineral": "矿物",
    "amorphous": "不定形",
    "water2": "水中2",
    "ditto": "百变怪",
    "dragon": "龙",
    "undiscovered": "未发现",
}

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
        for m in mon.moves:
            info = dex.moves.get(m) or {}
            mv.append(
                f"{info.get('zh') or info.get('name') or m}"
                f"({info.get('type') and dex.type_label(info['type'])}·{info.get('basePower') or '—'}"
                f"·PP{mon.pp.get(m, info.get('pp', 0))}/{info.get('pp', 0)})"
            )
        lines.append("   招式: " + "、".join(mv))
        if entry.get("evos") and not entry.get("battleOnly"):
            evo_names = "、".join(
                dex.species.get(x, {}).get("zh", x) for x in entry["evos"]
            )
            lines.append("   可进化: " + evo_names)
        return "\n".join(lines)

    @staticmethod
    def _parse_moves(raw: str) -> list[str]:
        if not raw:
            return []
        for sep in ("、", "，", ",", "/", "|"):
            raw = raw.replace(sep, ",")
        return [m.strip() for m in raw.split(",") if m.strip()]

    # ──────────────────────────── 图鉴工具 ────────────────────────────

    async def poke_dex_species(self, event, name: str) -> str:
        """查询宝可梦图鉴:属性、种族值、特性、进化、身高体重。

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
            lines.append(f"性别: 雄性 {87.5 - gr * 12.5:g}% / 雌性 {gr * 12.5:g}%")
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
        lines = [f"【{dex.species[key]['zh']} 可学招式】(共 {len(items)} 个)"]
        for it in items:
            m = dex.moves.get(it["move"]) or {}
            label = m.get("zh") or m.get("name") or it["move"]
            lines.append(f"- {label} [{','.join(it['methods'])}]")
        if len(lines) > 90:
            lines = lines[:90] + [f"…(其余 {len(items) - 89} 个已省略)"]
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
        """
        dex = get_dex()
        data = self._poke_load(event)
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
            replace(string): Optional. 要遗忘的第几个招式(1-4);留空且已满 4 个时会失败。
        """
        dex = get_dex()
        data = self._poke_load(event)
        found = self._find_member(data, target)
        if not found:
            return f"❌ 队伍里找不到「{target}」。"
        _idx, p = found
        mr = dex.resolve_move(move)
        if mr is None:
            return f"❌ 未找到招式「{move}」。"
        move_key = mr[0]
        known = dex.learnset(p.get("species", ""))
        if move_key not in known:
            # 允许教学/学习器补学存在但不强制,给出提示
            return (
                f"❌ {p.get('species')} 无法学会「{mr[1].get('zh')}」"
                f"(不在其可学招式表内)。"
            )
        mon = Pokemon.from_dict(p)
        if move_key in mon.moves:
            return f"⚠️ {mon.display} 已经会「{mr[1].get('zh')}」了。"
        if len(mon.moves) >= 4:
            if not replace or not str(replace).strip().isdigit():
                known_list = "、".join(
                    (dex.moves.get(m) or {}).get("zh", m) for m in mon.moves
                )
                return (
                    f"⚠️ {mon.display} 已会 4 个招式({known_list})。"
                    f"请用 replace 指定要遗忘的招式序号(1-4)。"
                )
            ri = int(replace) - 1
            if ri < 0 or ri >= len(mon.moves):
                return "❌ replace 序号无效(应为 1-4)。"
            forgotten = mon.moves[ri]
            mon.moves[ri] = move_key
            mon.pp.pop(move_key, None)
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
            mon.cur_hp = min(mon.cur_hp, mon.max_hp) or mon.max_hp
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

    async def poke_evolve(self, event, target: str, into: str = "") -> str:
        """让宝可梦进化(达到等级条件或指定进化形态)。

        Args:
            target(string): 队伍序号(1 起)或名称。
            into(string): Optional. 指定进化形态(中/英/标识);留空则按等级自动判断。
        """
        dex = get_dex()
        data = self._poke_load(event)
        found = self._find_member(data, target)
        if not found:
            return f"❌ 队伍里找不到「{target}」。"
        idx, p = found
        mon = Pokemon.from_dict(p)
        new_key = ""
        if into:
            r = dex.resolve_species(into)
            if r:
                new_key = r[0]
        else:
            new_key = dex.evolution(mon.species, mon.level) or ""
        if not new_key:
            return f"⚠️ {mon.display} 目前没有可用的等级进化。可用 into 指定进化形态。"
        old_zh = mon.entry.get("zh", mon.species)
        mon.species = new_key
        # 保留可学招式,补全新形态能力
        mon.stats = dex.compute_stats(new_key, mon.level, mon.ivs, mon.evs, mon.nature)
        mon.max_hp = mon.stats["hp"]
        mon.cur_hp = mon.max_hp
        mon.fainted = False
        mon.faint_logged = False
        # 特性若新形态不再拥有,换默认特性
        new_entry = dex.species.get(new_key, {})
        allowed = {
            (dex.resolve_ability(a) or ("", ""))[0]
            for a in (new_entry.get("abilities") or {}).values()
        }
        if mon.ability not in allowed:
            default = (new_entry.get("abilities") or {}).get("0")
            ar = dex.resolve_ability(default) if default else None
            mon.ability = ar[0] if ar else ""
        # 补充该形态可学但尚未掌握的招式(不自动替换)
        p.clear()
        p.update(mon.to_dict())
        self._poke_save(event, data)
        return (
            f"✨ 恭喜!{old_zh} 进化成了 {new_entry.get('zh', new_key)}!\n"
            + self._fmt_mon(mon, idx + 1)
        )

    async def poke_heal_party(self, event) -> str:
        """回复全队 HP 与异常状态(宝可梦中心)。

        Args: 无。
        """
        data = self._poke_load(event)
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
        msgs += self._gain_exp(mon, exp_gain)
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
                    msgs.append(
                        f"   {mon.display} 想学「{label}」,但招式已满(用 poke_learn_move 替换)。"
                    )
            evo = dex.evolution(mon.species, mon.level)
            if evo and not (dex.species.get(evo) or {}).get("battleOnly"):
                old_zh = mon.entry.get("zh", mon.species)
                mon.species = evo
                mon.stats = dex.compute_stats(
                    evo, mon.level, mon.ivs, mon.evs, mon.nature
                )
                mon.max_hp = mon.stats["hp"]
                mon.cur_hp = mon.max_hp
                mon.fainted = False
                mon.faint_logged = False
                msgs.append(
                    f"   ✨ {old_zh} 进化成了 {dex.species.get(evo, {}).get('zh', evo)}!"
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
        n = int(count or 1)
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

        # ── 进化石 ──
        if eff.get("evolve_stone"):
            new_key = self._stone_evolution(dex, mon.species, key)
            if not new_key:
                return f"⚠️ {mon.display} 对 {entry['zh']} 没有反应。"
            old = mon.display
            mon.species = new_key
            mon.stats = dex.compute_stats(new_key, mon.level, mon.ivs, mon.evs, mon.nature)
            mon.max_hp = mon.stats["hp"]
            mon.full_heal()
            msgs.append(f"✨ {old} 使用了 {entry['zh']},进化成了 {dex.species[new_key]['zh']}!")
        # ── 神奇糖果 ──
        elif eff.get("level_up"):
            if mon.level >= 100:
                return f"⚠️ {mon.display} 已经是 100 级。"
            mon.level = min(100, mon.level + int(eff["level_up"]))
            mon.stats = dex.compute_stats(mon.species, mon.level, mon.ivs, mon.evs, mon.nature)
            mon.max_hp = mon.stats["hp"]
            mon.full_heal()
            msgs.append(f"{mon.display} 升到了 Lv{mon.level}!")
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
            battle = Battle(
                player=Side(name="player", party=[mon]),
                enemy=Side(name="enemy", party=[]),
            )
            battle._apply_item_effect(mon, eff)
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
    def _stone_evolution(dex, species: str, stone_key: str) -> str:
        """找该宝可梦用对应进化石能变成的形态(比对 evoItem 英文名)。"""
        entry = dex.species.get(species) or {}
        zh = bag_item_label(stone_key)
        for e in entry.get("evos") or []:
            cand = dex.species.get(e) or {}
            if cand.get("battleOnly"):
                continue
            item = str(cand.get("evoItem") or "")
            if not item:
                continue
            if item == zh or item.lower().replace(" ", "-") == stone_key:
                return e
        return ""

    # ──────────────────────────── 对战工具 ────────────────────────────

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
    ) -> str:
        """开始一场宝可梦对战(单打)。对手以分号分隔多只组成训练家队伍。

        Args:
            enemy(string): 对手。单只写名称;多只用分号分隔,每项可写 "名称|等级|招式1+招式2"。例如 "皮卡丘|50|十万伏特+电光一闪;喷火龙|52"。
            enemy_level(int): Optional. 对手默认等级(每项未写等级时使用),默认 50。
            enemy_moves(string): Optional. 对手默认招式(逗号分隔),每项未写招式时使用。
            enemy_ability(string): Optional. 对手特性(仅单只时生效)。
            enemy_item(string): Optional. 对手道具(仅单只时生效)。
            weather(string): Optional. 开场天气: sun/rain/sand/snow 或 晴天/下雨/沙暴/下雪。
            terrain(string): Optional. 开场场地: electric/grassy/misty/psychic。
            wild(bool): Optional. 是否野生战(可投球捕获/可逃跑)。多只对手时自动视为训练家战;默认 true。
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
            if not mv and i == 0:
                mv = self._parse_moves(enemy_moves)
            try:
                enemy_party.append(
                    create_pokemon(
                        sp,
                        level=lv,
                        ability=enemy_ability if len(entries) == 1 else "",
                        item=enemy_item if len(entries) == 1 else "",
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
            wild=bool(wild) and len(enemy_party) == 1,
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
        # 同步我方队伍状态(HP/PP/异常)与背包
        data["party"] = [m.to_dict() for m in battle.player.party]
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
        if s.lower().startswith(("switch", "换人", "替换")):
            rest = s.split(" ", 1)[1] if " " in s else ""
            rest = rest.strip()
            if rest.isdigit():
                idx = int(rest) - 1
            else:
                data = {"party": [m.to_dict() for m in battle.player.party]}
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
