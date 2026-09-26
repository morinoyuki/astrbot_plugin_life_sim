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
    print("all pokesim tests passed")
