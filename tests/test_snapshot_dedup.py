"""/undo 快照去重(内容寻址版本表)测试:_compact_lore_versions / _resolve_snapshot_lore
/_compact_rpg_versions / _resolve_rpg_snapshot 的兼容性与数据一致性。

运行方式(在插件根目录):
    .venv/bin/python tests/test_snapshot_dedup.py
"""
import asyncio
import importlib.util
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
_spec = importlib.util.spec_from_file_location(
    "lsim_pkg", os.path.join(_ROOT, "__init__.py"),
    submodule_search_locations=[_ROOT],
)
_pkg = importlib.util.module_from_spec(_spec)
sys.modules["lsim_pkg"] = _pkg
_spec.loader.exec_module(_pkg)

from lsim_pkg.main import (
    _compact_lore_versions,
    _compact_rpg_versions,
    _resolve_rpg_snapshot,
    _resolve_snapshot_lore,
)


def _lore_snapshots(n):
    """构造 n 个内联格式 lore 快照;内容在 [0..2] 间循环。"""
    states = [
        {"world_lore": [], "character_lore": {}},
        {
            "world_lore": [{"seq": 1, "section": "地理", "content": "有森林"}],
            "character_lore": {"主角": [{"seq": 1, "section": "性格", "content": "勇敢"}]},
        },
        {
            "world_lore": [
                {"seq": 1, "section": "地理", "content": "有森林"},
                {"seq": 2, "section": "城市", "content": "有王都"},
            ],
            "character_lore": {
                "主角": [{"seq": 1, "section": "性格", "content": "勇敢"}],
                "花音": [{"seq": 1, "section": "外观", "content": "银发"}],
            },
        },
    ]
    return [
        {"turn": i + 1, **states[i % 3]} for i in range(n)
    ]


def _rpg_snapshots(n):
    """n 个内联 RPG 快照;chars/sessions 内容仅 2 组循环,scope 各不相同。"""
    state_a = {
        "chars": {"g1_阿": {"name": "阿", "hp": 10, "session_id": "s_a"}},
        "sessions": {"s_a": {"session_id": "s_a", "group_id": "g1"}},
    }
    state_b = {
        "chars": {
            "g1_阿": {"name": "阿", "hp": 20, "session_id": "s_a"},
            "g1_森": {"name": "森", "hp": 5, "session_id": "s_a"},
        },
        "sessions": {"s_a": {"session_id": "s_a", "group_id": "g1"}},
    }
    return [
        {
            "turn": i + 1,
            "scope": {"group_id": "g1", "sender_uid": f"u{i}"},
            **(state_a if i % 2 == 0 else state_b),
        }
        for i in range(n)
    ]


def test_lore_dedup_roundtrip():
    n = 25
    session = {"lore_snapshots": _lore_snapshots(n)}
    orig = json.loads(json.dumps(session["lore_snapshots"]))
    _compact_lore_versions(session)
    assert len(session["lore_snapshots"]) == n, "快照数量必须保持不变"
    assert len(session["_lore_versions"]) == 3, "内容只有 3 组,版本表应只有 3 条"
    for ref, s in zip(session["lore_snapshots"], orig):
        wl, cl = _resolve_snapshot_lore(session, ref)
        assert wl == s["world_lore"] and cl == s["character_lore"], (
            f"resolve 不一致 turn={ref.get('turn')}"
        )
    # 快照本身应已瘦身为 {turn, version}
    assert all(set(r.keys()) == {"turn", "version"} for r in session["lore_snapshots"])
    print("lore dedup roundtrip OK")


def test_lore_legacy_compat():
    """老会话(内联格式 + 无版本表)解析路径。"""
    session = {"lore_snapshots": _lore_snapshots(3)}
    _compact_lore_versions(session)
    # 模拟再读一遍:新格式快照 + 已有版本表 → 稳定幂等
    before = json.dumps(session, sort_keys=True)
    _compact_lore_versions(session)
    assert json.dumps(session, sort_keys=True) == before, "重复压缩必须幂等"
    print("lore legacy compat/idempotent OK")


def test_rpg_dedup_roundtrip():
    n = 25
    session = {"rpg_snapshots": _rpg_snapshots(n)}
    orig = json.loads(json.dumps(session["rpg_snapshots"]))
    _compact_rpg_versions(session)
    assert len(session["rpg_snapshots"]) == n
    assert len(session["_rpg_versions"]) == 2, "内容只有 2 组,版本表应只有 2 条"
    for ref, s in zip(session["rpg_snapshots"], orig):
        resolved = _resolve_rpg_snapshot(session, ref)
        assert resolved["chars"] == s["chars"], f"chars 不一致 turn={ref['turn']}"
        assert resolved["sessions"] == s["sessions"], f"sessions 不一致 turn={ref['turn']}"
        assert resolved["scope"] == s["scope"], f"scope 必须保留 turn={ref['turn']}"
    assert all(set(r.keys()) == {"turn", "scope", "version"} for r in session["rpg_snapshots"])
    print("rpg dedup roundtrip OK")


def test_rpg_legacy_compat():
    session = {"rpg_snapshots": _rpg_snapshots(4)}
    _compact_rpg_versions(session)
    before = json.dumps(session, sort_keys=True)
    _compact_rpg_versions(session)
    assert json.dumps(session, sort_keys=True) == before, "重复压缩必须幂等"
    print("rpg legacy compat/idempotent OK")


def test_rollback_prune_keeps_referenced_versions():
    """回滚裁剪快照后,版本表只保留仍被引用的内容。"""
    session = {"lore_snapshots": _lore_snapshots(25)}
    _compact_lore_versions(session)
    # 模拟 /undo 回滚到 turn=3(只留 1..3 的快照)
    session["lore_snapshots"] = [s for s in session["lore_snapshots"] if s["turn"] <= 3]
    _compact_lore_versions(session)
    # turn 1..3 内容 = states[0], states[1], states[2] → 3 组
    assert len(session["_lore_versions"]) == 3
    assert [s["turn"] for s in session["lore_snapshots"]] == [1, 2, 3]
    print("rollback prune keeps referenced versions OK")


async def main():
    test_lore_dedup_roundtrip()
    test_lore_legacy_compat()
    test_rpg_dedup_roundtrip()
    test_rpg_legacy_compat()
    test_rollback_prune_keeps_referenced_versions()
    print("ALL SNAPSHOT DEDUP TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
