"""宝可梦模式测试:图鉴数据 / 属性相克 / 招式学习 / 对战引擎 / poke_* 工具。

可单独运行:`.venv/bin/python tests/test_pokesim.py`
也可被 pytest 收集(内部用 asyncio.run,无需 pytest-asyncio)。
"""

import asyncio
import importlib.util
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
_spec = importlib.util.spec_from_file_location(
    "lsim_pkg",
    os.path.join(_ROOT, "__init__.py"),
    submodule_search_locations=[_ROOT],
)
_pkg = importlib.util.module_from_spec(_spec)
sys.modules["lsim_pkg"] = _pkg
_spec.loader.exec_module(_pkg)

from lsim_pkg.pokesim.dex import get_dex
from lsim_pkg.pokesim.engine import (
    battle_from_dict,
    battle_to_dict,
    create_pokemon,
    start_battle,
)
from lsim_pkg.pokesim.tools import PokemonMixin


class _Event:
    def __init__(self, gid="g1", sid="u1"):
        self.group_id = gid
        self.sender_id = sid

    def get_sender_id(self):
        return self.sender_id

    def get_group_id(self):
        return self.group_id


class _FakePlugin(PokemonMixin):
    def __init__(self, data_dir):
        self.data_dir = data_dir

    def _sim_session_key(self, event):
        return f"group_{event.group_id}" if event.group_id else f"user_{event.sender_id}"


# ──────────────────────────── 图鉴 ────────────────────────────


def test_dex_lookup_and_types():
    dex = get_dex()
    key, entry = dex.resolve_species("皮卡丘")
    assert key == "pikachu"
    assert entry["types"] == ["Electric"]
    assert dex.resolve_species("Pikachu")[0] == "pikachu"
    assert dex.resolve_move("十万伏特")[0] == "thunderbolt"
    assert dex.resolve_ability("静电")[0] == "static"
    assert dex.type_multiplier("Electric", ["Water", "Flying"]) == 4.0
    assert dex.type_multiplier("Ground", ["Flying"]) == 0.0
    assert dex.type_multiplier("Normal", ["Ghost"]) == 0.0
    # 图鉴说明文字:中文优先,英文兼有
    assert entry.get("flavor")
    assert entry.get("flavorEn")
    assert "尾巴" in entry["flavor"] or "电" in entry["flavor"]
    covered = sum(1 for v in dex.species.values() if v.get("flavor"))
    assert covered > 900
    assert all(v.get("flavorEn") for v in dex.species.values())
    # 性别比例(雌性 = genderRate/8)与蛋群翻译
    assert entry["genderRate"] == 4  # 皮卡丘 50/50
    assert dex.species["bulbasaur"]["genderRate"] == 1  # 妙蛙种子 87.5/12.5
    assert dex.species["pikachu"]["eggGroups"] == ["ground", "fairy"]


def test_dex_stats_and_learnset():
    dex = get_dex()
    stats = dex.compute_stats("pikachu", 50)
    assert stats["hp"] > 0 and stats["spe"] >= stats["atk"]
    # 进化后形态借用基础形态的学习表
    assert "thunderbolt" in dex.learnset("raichualola")
    assert "thunderbolt" in dex.learnset("pikachu")
    assert len(dex.learnable("meowscarada", 50)) > 10
    assert dex.evolution("magikarp", 20) == "gyarados"
    assert dex.evolution("magikarp", 10) is None


# ──────────────────────────── 引擎 ────────────────────────────


def _run_battle(seed):
    p = create_pokemon("皮卡丘", 50, moves=["thunderbolt"])
    o = create_pokemon("小拉达", 50, moves=["tackle"])
    b = start_battle([p], [o], seed=seed)
    b.start()
    logs = []
    for _ in range(20):
        if b.finished:
            break
        logs.extend(b.step({"type": "move", "move": "thunderbolt"}))
    return b, logs


def test_engine_deterministic():
    b1, l1 = _run_battle(123)
    b2, l2 = _run_battle(123)
    assert l1 == l2
    assert b2.winner == b1.winner
    assert b1.finished and b1.winner == "player"


def test_engine_tera_and_switch():
    p1 = create_pokemon("皮卡丘", 50, moves=["thunderbolt"])
    p2 = create_pokemon("妙蛙种子", 50, moves=["tackle"])
    enemy = create_pokemon("小拉达", 50, moves=["tackle"])
    b = start_battle([p1, p2], [enemy], seed=1)
    b.start()
    b.step({"type": "move", "move": "thunderbolt", "tera": True})
    assert b.player.mon.terastallized is True
    assert b.player.tera_used is True
    # 换人
    b.step({"type": "switch", "index": 1})
    assert b.player.active == 1


def test_engine_serialization():
    p = create_pokemon("喷火龙", 60, moves=["flamethrower"])
    e = create_pokemon("妙蛙花", 60, moves=["solarbeam"])
    b = start_battle([p], [e], seed=9)
    b.start()
    b.step({"type": "move", "move": "flamethrower"})
    d = battle_to_dict(b)
    b2 = battle_from_dict(d)
    assert b2.turn == b.turn
    assert b2.player.mon.cur_hp == b.player.mon.cur_hp
    assert b2.enemy.mon.species == "venusaur"


# ──────────────────────────── 工具 ────────────────────────────


def _scenario():
    with tempfile.TemporaryDirectory() as tmp:
        p = _FakePlugin(tmp)
        ev = _Event()

        async def run():
            out = await p.poke_add_pokemon(
                ev, "皮卡丘", level=30, moves="quickattack,thundershock,growl"
            )
            assert "皮卡丘" in out
            team = await p.poke_team(ev)
            assert "皮卡丘" in team

            # 学新招(未满 4 个)
            learned = await p.poke_learn_move(ev, "1", "十万伏特")
            assert "学会了" in learned
            # 现在 4 个,再学需要 replace
            full = await p.poke_learn_move(ev, "1", "grassknot")
            assert "replace" in full
            rep = await p.poke_learn_move(ev, "1", "grassknot", replace="1")
            assert "学会了" in rep

            # 图鉴工具
            dex_out = await p.poke_dex_species(ev, "皮卡丘")
            assert "图鉴说明" in dex_out
            assert "雄性 50% / 雌性 50%" in dex_out
            assert "蛋群: 陆上/妖精" in dex_out
            assert "电" in dex_out
            mv = await p.poke_dex_move(ev, "十万伏特")
            assert "90" in mv
            tm = await p.poke_type_matchup(ev, "电", "水,飞行")
            assert "4" in tm

            # 编辑
            edited = await p.poke_edit_pokemon(ev, "1", level=50, item="生命宝珠")
            assert "等级→50" in edited

            # 对战
            start = await p.poke_battle_start(ev, "小拉达|10")
            assert "小拉达" in start
            turn = await p.poke_battle_turn(ev, "move 十万伏特")
            assert "十万伏特" in turn
            status = await p.poke_battle_status(ev)
            assert "我方" in status

            # 快照 / 回滚接口
            snap = p.pokemon_capture(ev)
            assert snap["data"] and len(snap["data"]["party"]) == 1
            p.pokemon_apply({"scope": snap["scope"], "data": None})
            empty = await p.poke_team(ev)
            assert "空" in empty or "队伍(0" in empty
            p.pokemon_apply(snap)
            restored = await p.poke_team(ev)
            assert "皮卡丘" in restored

        asyncio.run(run())


def test_pokemon_tools():
    _scenario()


def _items_scenario():
    with tempfile.TemporaryDirectory() as tmp:
        p = _FakePlugin(tmp)
        ev = _Event()

        async def run():
            await p.poke_add_pokemon(ev, "皮卡丘", level=30, moves="thunderbolt,quickattack,growl")
            await p.poke_add_pokemon(ev, "六尾", level=20, moves="ember,quickattack")

            # 背包
            bag = await p.poke_bag(ev)
            assert "空" in bag
            added = await p.poke_bag(ev, item="精灵球", count=5)
            assert "精灵球×5" in added
            await p.poke_bag(ev, item="大师球", count=1)
            await p.poke_bag(ev, item="伤药", count=3)
            await p.poke_bag(ev, item="火之石", count=1)
            await p.poke_bag(ev, item="神奇糖果", count=2)

            # 野生战 + 大师球必中捕获
            start = await p.poke_battle_start(ev, "绿毛虫|5")
            assert "野生对战" in start
            assert "可用招式: 1." in start  # 战斗中展示可用招式
            turn = await p.poke_battle_turn(ev, "catch 大师球")
            assert "成功捕获了" in turn
            team = await p.poke_team(ev)
            assert "绿毛虫" in team  # 自动入队

            # 战斗中使用道具(合法:回复类) + 逃走
            await p.poke_battle_start(ev, "小拉达|30")
            bad = await p.poke_battle_turn(ev, "item 精灵球")
            assert "catch" in bad  # 精灵球应提示用 catch
            await p.poke_battle_turn(ev, "switch 2")
            heal = await p.poke_battle_turn(ev, "item 伤药")
            assert "伤药" in heal
            await p.poke_battle_start(ev, "绿毛虫|5")
            run = await p.poke_battle_turn(ev, "run")
            assert "成功逃" in run

            # 战斗外道具:神奇糖果升级
            lv_out = await p.poke_use_item(ev, "1", "神奇糖果")
            assert "Lv31" in lv_out

            # 进化石
            stone = await p.poke_use_item(ev, "六尾", "火之石")
            assert "九尾" in stone

            # 非野生战不能捕获/逃跑
            await p.poke_battle_start(ev, "小拉达|10;波波|10")
            caught = await p.poke_battle_turn(ev, "catch 精灵球")
            assert "野生" in caught

        asyncio.run(run())


def test_pokemon_items_and_catch():
    _items_scenario()


def _train_pvp_scenario():
    with tempfile.TemporaryDirectory() as tmp:
        p = _FakePlugin(tmp)
        a = _Event(sid="1001")
        b = _Event(sid="1002")

        async def run():
            await p.poke_trainer(a, "小智")
            await p.poke_trainer(b, "小茂")
            await p.poke_add_pokemon(
                a, "皮卡丘", level=5, moves="thundershock,quickattack,growl"
            )
            await p.poke_add_pokemon(
                b, "小火龙", level=5, moves="ember,scratch,growl"
            )
            trainers = await p.poke_trainers(a)
            assert "小智" in trainers and "小茂" in trainers

            # 训练:经验 + 努力值 + 升级
            tr = await p.poke_train(a, "1", sessions=20, focus="速度")
            assert "努力值" in tr and "升到了" in tr
            team = await p.poke_team(a)
            assert "经验" in team

            # PvP
            start = await p.poke_battle_pvp(a, "1002")
            assert "PvP" in start
            for _ in range(30):
                st = await p.poke_battle_status(a)
                if "战斗结束" in st:
                    break
                r = await p.poke_battle_turn(a, "move thundershock")
                if "战斗结束" in r:
                    break
            # 对手队伍被写回(战斗后 HP 不是满的)
            bteam = await p.poke_team(b)
            assert "小火龙" in bteam

        asyncio.run(run())


def test_pokemon_train_and_pvp():
    _train_pvp_scenario()


def test_evolution_methods():
    from pokesim.dex import get_dex

    d = get_dex()

    def met(sp, **kw):
        return {o["target"] for o in d.evolution_options(sp, **kw) if o["met"]}

    # 等级
    assert "charmeleon" in met("charmander", level=16)
    assert "charmeleon" not in met("charmander", level=15)
    # 亲密度
    assert "pikachu" in met("pichu", level=10, friendship=160)
    assert "pikachu" not in met("pichu", level=10, friendship=100)
    # 使用道具
    assert "ninetales" in met("vulpix", item="fire-stone")
    assert "flapple" in met("applin", item="tart-apple")
    assert "appletun" not in met("applin", item="tart-apple")
    # 交换(可携带道具)
    assert "machamp" in met("machoke", trade=True)
    assert "steelix" in met("onix", trade=True, item="metal-coat")
    assert "steelix" not in met("onix", trade=True, item="dragon-scale")
    # 升级时学会特定招式
    assert "ambipom" in met("aipom", level=32, moves=["doublehit"])
    assert "ambipom" not in met("aipom", level=32, moves=["swift"])
    # 携带物 + 昼夜
    assert "gliscor" in met("gligar", level=40, item="razor-fang", daytime="night")
    assert "gliscor" not in met("gligar", level=40, item="razor-fang", daytime="day")
    # 特殊(妖精招式)
    assert "sylveon" in met("eevee", level=20, friendship=200, moves=["babydolleyes"])
    assert "sylveon" not in met("eevee", level=20, friendship=50, moves=["babydolleyes"])
    # 昼夜分支
    assert "espeon" in met("eevee", level=20, friendship=200, daytime="day")
    assert "umbreon" not in met("eevee", level=20, friendship=200, daytime="day")
    assert "umbreon" in met("eevee", level=20, friendship=200, daytime="night")
    # 性别限制
    assert not met("salandit", level=40, gender="M")
    assert "salazzle" in met("salandit", level=40, gender="F")
    # 能力值分支
    assert "hitmonlee" in met("tyrogue", level=20, stats={"atk": 50, "def": 40})
    assert "hitmonchan" in met("tyrogue", level=20, stats={"atk": 40, "def": 50})
    assert "hitmontop" in met("tyrogue", level=20, stats={"atk": 45, "def": 45})
    # 悬空引用已清零
    for k, v in d.species.items():
        for e in v.get("evos") or []:
            assert e in d.species, f"{k} -> {e} missing"


def _evo_tool_scenario():
    with tempfile.TemporaryDirectory() as tmp:
        p = _FakePlugin(tmp)
        ev = _Event()

        async def run():
            # 交换进化
            await p.poke_add_pokemon(ev, "豪力", level=30, moves="karatechop")
            out = await p.poke_evolve(ev, "1", trade=True)
            assert "怪力" in out
            # 携带物 + 夜晚
            await p.poke_add_pokemon(ev, "天蝎", level=40, moves="slash")
            await p.poke_edit_pokemon(ev, "2", item="razor-fang")
            out = await p.poke_evolve(ev, "2", daytime="night")
            assert "天蝎王" in out
            # 使用进化石
            await p.poke_add_pokemon(ev, "六尾", level=20, moves="ember")
            await p.poke_bag(ev, item="火之石", count=1)
            out = await p.poke_use_item(ev, "3", "火之石")
            assert "九尾" in out
            # 性别限制
            await p.poke_add_pokemon(ev, "夜盗火蜥", level=40, moves="ember", gender="M")
            out = await p.poke_evolve(ev, "4", into="焰后蜥")
            assert "还不能进化" in out
            # 多分支需指定
            await p.poke_add_pokemon(ev, "伊布", level=20, moves="quickattack")
            out = await p.poke_evolve(ev, "5")
            assert "分支" in out or "不满足" in out

        asyncio.run(run())


def test_pokemon_evolution():
    test_evolution_methods()
    _evo_tool_scenario()


def _move_replace_scenario():
    with tempfile.TemporaryDirectory() as tmp:
        p = _FakePlugin(tmp)
        ev = _Event()

        async def run():
            await p.poke_add_pokemon(ev, "皮卡丘", level=5, moves="thundershock,growl,tailwhip")
            r = await p.poke_train(ev, "1", sessions=30)
            # 满 4 招时提示应带序号与招式名
            full = [ln for ln in r.splitlines() if "招式已满" in ln]
            assert full, r
            assert "1." in full[0] and "replace" in full[0]
            # 队伍招式带序号
            team = await p.poke_team(ev)
            assert "招式: 1." in team
            # 按招式名遗忘
            out = await p.poke_learn_move(ev, "1", move="十万伏特", replace="摇尾巴")
            assert "忘记了" in out and "十万伏特" in out
            # 按序号遗忘
            out = await p.poke_learn_move(ev, "1", move="电光一闪", replace="1")
            assert "学会了「电光一闪」" in out
            # 非法 replace 应拒绝
            out = await p.poke_learn_move(ev, "1", move="打雷", replace="不存在的招")
            assert "replace" in out and "序号" in out

        asyncio.run(run())


def test_pokemon_move_replace():
    _move_replace_scenario()


def test_pokemon_battle_pp_rules():
    with tempfile.TemporaryDirectory() as tmp:
        p = _FakePlugin(tmp)
        ev = _Event()

        async def run():
            await p.poke_add_pokemon(ev, "皮卡丘", level=20, moves="thundershock,quickattack")
            await p.poke_battle_start(ev, "小拉达|15")
            # 只能用已学会的招式
            out = await p.poke_battle_turn(ev, "move 十万伏特")
            assert "不会使用" in out
            # 单招 PP 耗尽 -> 拒绝(还有其他可用招)
            data = p._poke_load(ev)
            data["battle"]["player"]["party"][0]["pp"]["thundershock"] = 0
            p._poke_save(ev, data)
            out = await p.poke_battle_turn(ev, "move 电击")
            assert "PP 已耗尽" in out
            # 全部 PP 耗尽 -> 强制挣扎(无属性、反作用)
            data = p._poke_load(ev)
            for m in data["battle"]["player"]["party"][0]["moves"]:
                data["battle"]["player"]["party"][0]["pp"][m] = 0
            p._poke_save(ev, data)
            out = await p.poke_battle_turn(ev, "move 电击")
            assert "挣扎" in out and "反作用力" in out

        asyncio.run(run())


def test_pokemon_learnset_display():
    with tempfile.TemporaryDirectory() as tmp:
        p = _FakePlugin(tmp)
        ev = _Event()

        async def run():
            out = await p.poke_learnset(ev, "皮卡丘")
            assert "升级 Lv" in out and "─ 升级招式 ─" in out
            assert "十万伏特" in out
            lv30 = await p.poke_learnset(ev, "皮卡丘", level=30)
            assert "打雷" not in lv30.split("─ 升级招式 ─")[1].split("─")[0]  # Lv44 尚未学会
            only_lv = await p.poke_learnset(ev, "皮卡丘", include_tm=False)
            assert "学习器·教学 0" in only_lv

        asyncio.run(run())


def test_pokemon_catch_only_wild():
    with tempfile.TemporaryDirectory() as tmp:
        p = _FakePlugin(tmp)
        ev = _Event()

        async def run():
            await p.poke_add_pokemon(ev, "皮卡丘", level=25, moves="thundershock,quickattack")
            await p.poke_bag(ev, item="精灵球", count=5)
            # 单只对手 + trainer=true -> 训练家战,不可捕获/逃跑
            start = await p.poke_battle_start(ev, "小拉达|10", trainer=True)
            assert "训练家对战" in start
            out = await p.poke_battle_turn(ev, "catch 精灵球")
            assert "野生对战" in out
            out = await p.poke_battle_turn(ev, "run")
            assert "逃走" in out
            # 单只对手 + wild=false 也视为训练家战
            start = await p.poke_battle_start(ev, "小拉达|10", wild=False)
            assert "训练家对战" in start
            # 多只自动训练家战
            start = await p.poke_battle_start(ev, "小拉达|10;波波|10")
            assert "训练家对战" in start
            # 默认(野生)可捕获
            start = await p.poke_battle_start(ev, "绿毛虫|5")
            assert "野生对战" in start
            out = await p.poke_battle_turn(ev, "catch 精灵球")
            assert "投出了" in out or "成功捕获" in out

        asyncio.run(run())


def test_encounter_module():
    import random

    from lsim_pkg.pokesim.encounter import biome_types, roll_encounter, roll_level

    dex = get_dex()
    # 生态属性解析
    assert "Water" in biome_types("海面")
    assert "Bug" in biome_types("夜晚的森林") or "Grass" in biome_types("夜晚的森林")
    # 地区限定 + 生态过滤
    enc = roll_encounter(dex, area="水面", region="丰缘", rng=random.Random(1))
    assert enc is not None and "Water" in enc["types"]
    num = int(dex.species[enc["species"]]["num"])
    assert 252 <= num <= 386
    # 默认排除传说/幻兽
    for seed in range(60):
        e = roll_encounter(dex, area="", rng=random.Random(seed))
        assert not dex.species[e["species"]].get("isLegendary")
        assert not dex.species[e["species"]].get("isMythical")
    # 等级浮动
    lvl = roll_level(dex, [{"level": 20}], rng=random.Random(0))
    assert 17 <= lvl <= 22
    assert roll_level(dex, [{"level": 20}], level=42) == 42


def test_pokemon_wild_encounter():
    with tempfile.TemporaryDirectory() as tmp:
        p = _FakePlugin(tmp)
        ev = _Event()

        async def run():
            assert "队伍是空的" in await p.poke_wild_encounter(ev, area="草地")
            await p.poke_add_pokemon(ev, "皮卡丘", level=15, moves="thundershock,quickattack")
            out = await p.poke_wild_encounter(ev, area="草地", region="关都", level=10)
            assert "野生的" in out and "野生战" in out and "Lv10" in out
            out = await p.poke_wild_encounter(ev, area="海面", region="丰缘")
            assert "野生的" in out and "野生战" in out

        asyncio.run(run())


def test_trainer_team_generation():
    import random

    from lsim_pkg.pokesim.trainer import generate_team, tier_of

    dex = get_dex()
    # 训练家级别解析
    assert tier_of("冠军")[0] == (5, 6)
    assert tier_of("短裤小子")[0] == (1, 2)
    assert tier_of("精英训练家")[0] == (2, 4)
    # 岩石主题:队伍均有岩石属性
    gen = generate_team(
        dex, party=[{"level": 20}], trainer="岩石道馆馆主", rng=random.Random(3)
    )
    assert gen["size"] >= 3
    assert "Rock" in gen["theme"]
    for m in gen["team"]:
        assert "Rock" in dex.species[m["species"]]["types"]
        assert 18 <= m["level"] <= 24
    assert gen["team"][-1]["is_ace"]
    # 地区限定:关都岩石道馆
    kanto = generate_team(
        dex,
        party=[{"level": 20}],
        trainer="岩石道馆馆主",
        region="关都",
        rng=random.Random(5),
    )
    assert kanto["size"] >= 3
    for m in kanto["team"]:
        e = dex.species[m["species"]]
        assert "Rock" in e["types"]
        assert 1 <= int(e["num"]) <= 151
    # 等级跟随队首 + 难度修正
    easy = generate_team(dex, party=[{"level": 30}], trainer="训练家", difficulty="easy", rng=random.Random(1))
    hard = generate_team(dex, party=[{"level": 30}], trainer="训练家", difficulty="hard", rng=random.Random(1))
    assert hard["level"] == easy["level"] + 4
    # 显式等级固定
    fixed = generate_team(dex, party=[{"level": 30}], trainer="道馆", level=42, rng=random.Random(2))
    assert fixed["level"] == 42
    # 默认不出传说/幻兽
    for seed in range(30):
        g = generate_team(dex, party=[{"level": 50}], trainer="冠军", rng=random.Random(seed))
        for m in g["team"]:
            e = dex.species[m["species"]]
            assert not e.get("isLegendary") and not e.get("isMythical")
    # 地点分布:队伍物种来自该地出现过的宝可梦
    pool = {p["species"] for p in dex.location_pools("viridian-forest", include_special=True)}
    g = generate_team(
        dex,
        party=[{"level": 20}],
        trainer="捕虫少年",
        location="viridian-forest",
        size=2,
        rng=random.Random(7),
    )
    assert g["size"] == 2
    assert all(m["species"] in pool for m in g["team"])
    # 指定王牌
    g = generate_team(
        dex,
        party=[{"level": 25}],
        trainer="岩石道馆馆主",
        location="mt-moon",
        ace="大岩蛇",
        rng=random.Random(4),
    )
    assert g["team"][-1]["species"] == "onix"
    assert g["team"][-1]["is_ace"]
    assert sum(1 for m in g["team"] if m["is_ace"]) == 1


def test_pokemon_trainer_battle():
    with tempfile.TemporaryDirectory() as tmp:
        p = _FakePlugin(tmp)
        ev = _Event()

        async def run():
            assert "队伍是空的" in await p.poke_trainer_battle(ev, trainer="短裤小子")
            await p.poke_add_pokemon(ev, "皮卡丘", level=12, moves="thundershock,quickattack")
            out = await p.poke_trainer_battle(ev, trainer="短裤小子")
            assert "派出了" in out and "训练家战" in out and "基准 Lv11" in out
            await p.poke_heal_party(ev)
            out = await p.poke_trainer_battle(ev, trainer="岩石道馆馆主小刚")
            assert "主题:岩石" in out and "基准 Lv13" in out
            await p.poke_heal_party(ev)
            out = await p.poke_trainer_battle(
                ev, trainer="捕虫少年", location="常青森林", region="关都"
            )
            assert "地点:常青森林" in out
            await p.poke_heal_party(ev)
            out = await p.poke_trainer_battle(ev, trainer="宿敌", members="杰尼龟|13|水枪;小火龙|13")
            assert "剧情指定" in out and "杰尼龟" in out

        asyncio.run(run())


def test_location_module():
    import random

    from lsim_pkg.pokesim.encounter import roll_location_encounter

    dex = get_dex()
    assert dex.find_location("常青森林") == "viridian-forest"
    assert dex.find_location("Viridian Forest") == "viridian-forest"
    assert dex.find_location("常青森林", "关都") == "viridian-forest"
    assert dex.find_location("常青森林", "kanto") == "viridian-forest"
    assert dex.resolve_region("关都") == "kanto"
    assert dex.resolve_region("伽勒尔") == "galar"
    assert dex.find_location("不存在的地方XYZ") == ""
    # 默认排除定点/赠予类
    pools = dex.location_pools("viridian-forest", "red-blue")
    assert pools and all(p["method"] != "static" for p in pools)
    assert any(p["species"] == "pikachu" for p in pools)
    # 定点/赠予可按需包含
    special = dex.location_pools("sinnoh-route-201", "platinum", include_special=True)
    assert any(p["method"] in ("gift", "static") for p in special)
    # 真实分布抽取
    enc = roll_location_encounter(
        dex, "viridian-forest", version_group="red-blue", rng=random.Random(1)
    )
    assert enc and enc["location"] == "viridian-forest"
    assert 3 <= enc["level"] <= 9
    keys = {p["species"] for p in dex.location_pools("viridian-forest", "red-blue")}
    assert enc["species"] in keys
    # 最低限度覆盖
    assert len(dex.locations) > 500
    assert sum(1 for v in dex.locations.values() if v["zh"]) > 400


def test_pokemon_location_tools():
    with tempfile.TemporaryDirectory() as tmp:
        p = _FakePlugin(tmp)
        ev = _Event()

        async def run():
            out = await p.poke_dex_location(ev, region="关都")
            assert "关都" in out and "地点" in out
            out = await p.poke_dex_location(ev, name="常青森林", version_group="red-blue")
            assert "常青森林" in out and "皮卡丘" in out and "%" in out
            assert "未知" not in out
            await p.poke_add_pokemon(ev, "皮卡丘", level=20, moves="thundershock,quickattack")
            out = await p.poke_wild_encounter(ev, area="常青森林", region="关都", version_group="red-blue")
            assert "野生的" in out and "该地等级" in out and "野生战" in out

        asyncio.run(run())


def test_pokemon_sprites():
    from lsim_pkg.pokesim.sprites import (
        available_count,
        pokemon_avatars_for_text,
        speaker_names,
        sprite_for_name,
        sprite_path,
    )

    assert available_count() > 1000
    p = sprite_path("pikachu")
    assert p and os.path.exists(p)
    with open(p, "rb") as fh:
        assert fh.read(8) == b"\x89PNG\r\n\x1a\n"
    # 中/英/别号都能命中
    assert sprite_for_name("皮卡丘")
    assert sprite_for_name("Pikachu")
    assert sprite_for_name("皮卡丘(小智的)")
    # 形态回退到本体(直接映射缺失时)
    assert sprite_for_name("坚盾剑怪")
    # 非宝可梦不误配
    assert sprite_for_name("小刚") == ""
    assert sprite_for_name("") == ""
    text = '<d name="皮卡丘">皮卡皮卡</d>\n<d name="小智">去吧!</d>\n<d name="皮卡丘">皮卡</d>'
    assert speaker_names(text) == ["皮卡丘", "小智"]
    av = pokemon_avatars_for_text(text, {"小智": "/tmp/x.png"})
    assert av["小智"] == "/tmp/x.png"  # 用户头像优先
    assert av["皮卡丘"].endswith("pikachu.png")


def test_tool_arg_coercion():
    """LLM 传垃圾参数时应被安全转换或拒绝,绝不抛异常。"""
    import lsim_pkg.main as M

    assert M._coerce_int_arg("3") == 3
    assert M._coerce_int_arg("3.7") == 3
    assert M._coerce_int_arg(True) == 1
    assert M._coerce_int_arg(None) is None
    assert M._coerce_int_arg("abc") is None
    assert M._coerce_bool_arg("true") is True
    assert M._coerce_bool_arg("no") is False
    assert M._coerce_bool_arg(1) is True
    assert M._coerce_str_arg(None) == ""
    assert M._coerce_str_arg(5) == "5"

    int_schema = {"properties": {"level": {"type": "integer"}}}
    assert M._coerce_tool_kwargs(int_schema, {"level": "12"}) == ({"level": 12}, None)
    fixed, err = M._coerce_tool_kwargs(int_schema, {"level": "abc"})
    assert fixed is None and "整数" in err
    # None 的整数交默认值处理;None 的字符串转空串(保留必填)
    assert M._coerce_tool_kwargs(int_schema, {"level": None}) == ({}, None)
    str_schema = {"properties": {"nickname": {"type": "string"}}}
    assert M._coerce_tool_kwargs(str_schema, {"nickname": None}) == ({"nickname": ""}, None)
    assert M._coerce_tool_kwargs(str_schema, {"nickname": 42}) == ({"nickname": "42"}, None)
    # 未声明的参数(如 event)原样透传
    assert M._coerce_tool_kwargs(int_schema, {"event": object()}) != (None, None)


def test_engine_bugfixes():
    """审计修复回归:灼伤倍率/状态免疫/毒菱/太晶爆发/会心数值来源/换人/PP/随机数。"""
    dex = get_dex()

    def mk(s, lv=50, **k):
        return create_pokemon(s, lv, **k)

    # H1 灼伤只乘一次(物理伤害 ×0.5,而非 ×0.25)
    b = start_battle([mk("machop", ability="no-guard")], [mk("snorlax")])
    att, foe, ent = b.player.mon, b.enemy.mon, dex.moves["karatechop"]
    healthy = b._calc_damage(att, foe, "karatechop", ent, 50, "Fighting", 1.0, False)
    att.status = "brn"
    burned = b._calc_damage(att, foe, "karatechop", ent, 50, "Fighting", 1.0, False)
    assert 0.4 <= burned / healthy <= 0.6

    # M2 魔法防守不阻止异常状态本身;M3 冰免冰/电免麻
    b = start_battle([mk("clefable", ability="magic-guard")], [mk("snorlax")])
    b._inflict(b.player.mon, "tox")
    assert b.player.mon.status == "tox"
    b = start_battle([mk("lapras")], [mk("snorlax")])
    b._inflict(b.player.mon, "frz")
    assert b.player.mon.status == ""
    b = start_battle([mk("pikachu")], [mk("snorlax")])
    b._inflict(b.player.mon, "par")
    assert b.player.mon.status == ""

    # M4 毒菱对钢属性无效
    b = start_battle([mk("steelix")], [mk("snorlax")])
    b.player.hazards["toxicspikes"] = 2
    b._apply_hazards(b.player, b.player.mon)
    assert b.player.mon.status == ""

    # M6 太晶爆发:太晶化后威力 100、按攻/特攻较高者决定分类
    b = start_battle([mk("machamp")], [mk("snorlax")])
    m = b.player.mon
    m.terastallized = True
    m.tera_type = "Fighting"
    assert b._effective_power(m, b.enemy.mon, "terablast", dex.moves["terablast"]) == 100

    # M7 会心不改变「扑击」的数值来源(用防御)
    b = start_battle([mk("snorlax")], [mk("pidgey")])
    bp = b.player.mon
    bp.stages["def"] = 6
    n = b._calc_damage(bp, b.enemy.mon, "bodypress", dex.moves["bodypress"], 80, "Fighting", 1.0, False)
    c = b._calc_damage(bp, b.enemy.mon, "bodypress", dex.moves["bodypress"], 80, "Fighting", 1.0, True)
    assert 1.2 <= c / n <= 1.8

    # H2 不能换上已倒下的宝可梦
    p = [mk("pikachu"), mk("geodude"), mk("pidgey")]
    b = start_battle(p, [mk("snorlax")])
    b.player.party[1].cur_hp = 0
    b.player.party[1].fainted = True
    b._do_switch(b.player, 1)
    assert b.player.active == 0

    # H3 换上后又被陷阱打倒时能继续处理
    b = start_battle([mk("pikachu")], [mk("geodude"), mk("pidgey"), mk("snorlax")])
    b.enemy.hazards["stealthrock"] = 1
    b.enemy.party[0].cur_hp = 1
    b.enemy.party[1].cur_hp = 1
    b.enemy.party[0].fainted = True
    b._check_faints()
    # 第 1 只倒下 → 派第 2 只 → 又被陷阱打倒 → 应继续派第 3 只
    assert b.enemy.active == 2
    assert not b.enemy.party[2].fainted

    # N 元气之粉只回复一个招式
    b = start_battle([mk("pikachu")], [mk("snorlax")])
    mon = b.player.mon
    mon.pp[mon.moves[0]] = 1
    mon.pp[mon.moves[1]] = 1
    b._apply_item_effect(mon, {"pp_restore": 10})
    depleted = [m for m in mon.moves if mon.pp[m] == 1]
    assert len(depleted) == 1

    # M1 同 salt 的随机数不再完全相关
    b = start_battle([mk("pikachu")], [mk("snorlax")])
    b.turn = 1
    b._rng_calls = 0
    assert b._rng(6).random() != b._rng(6).random()


if __name__ == "__main__":
    test_dex_lookup_and_types()
    test_dex_stats_and_learnset()
    test_engine_deterministic()
    test_engine_tera_and_switch()
    test_engine_serialization()
    test_pokemon_tools()
    test_pokemon_items_and_catch()
    test_pokemon_train_and_pvp()
    test_pokemon_evolution()
    test_pokemon_move_replace()
    test_pokemon_battle_pp_rules()
    test_pokemon_learnset_display()
    test_pokemon_catch_only_wild()
    test_encounter_module()
    test_pokemon_wild_encounter()
    test_trainer_team_generation()
    test_pokemon_trainer_battle()
    test_location_module()
    test_pokemon_location_tools()
    test_pokemon_sprites()
    test_tool_arg_coercion()
    test_engine_bugfixes()
    print("all pokesim tests passed")
