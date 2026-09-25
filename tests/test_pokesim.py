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


if __name__ == "__main__":
    test_dex_lookup_and_types()
    test_dex_stats_and_learnset()
    test_engine_deterministic()
    test_engine_tera_and_switch()
    test_engine_serialization()
    test_pokemon_tools()
    print("all pokesim tests passed")
