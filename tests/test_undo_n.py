"""/undo N 剧情历史回滚测试:
1) 理想情形(每轮 /do 都成功)
2) 旧版缺陷情形——失败轮推高了 lore_turn,但没产生 user 消息/剧情记录
   (老会话消息上无 turn 戳),/undo N 按消息数回滚时应精确删 N 条。
"""
import asyncio
import importlib.util
import os
import shutil
import sys
import tempfile
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
_spec = importlib.util.spec_from_file_location(
    "lsim_pkg", os.path.join(_ROOT, "__init__.py"),
    submodule_search_locations=[_ROOT],
)
_pkg = importlib.util.module_from_spec(_spec)
sys.modules["lsim_pkg"] = _pkg
_spec.loader.exec_module(_pkg)

from lsim_pkg.main import LifeSimPlugin, _narrative_branch
from lsim_pkg.storage_narrative import NarrativeStore
from lsim_pkg.storage_sim import SimStore


class _Event:
    def __init__(self, gid, sid):
        self.group_id = gid
        self.sender_id = sid
        self.message_obj = type("o", (), {"group_id": gid, "timestamp": time.time()})()
    def get_sender_id(self): return self.sender_id
    def get_sender_name(self): return "Tester"
    def plain_result(self, text): return type("R", (), {"text": text})()


class _FakePlugin:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.sim_store = SimStore(data_dir)
        self.narrative_store = NarrativeStore(data_dir)
    def _sim_session_key(self, event):
        return f"group_{event.group_id}" if event.group_id else f"user_{event.sender_id}"
    def _rpg_restore(self, snapshot):
        return {"restored_chars": 0, "restored_sessions": 0, "deleted_chars": 0, "deleted_sessions": 0}
    @staticmethod
    def _legacy_rollback_target_turn(session, take, user_turns):
        return LifeSimPlugin._legacy_rollback_target_turn(session, take, user_turns)
    async def _restore_narrative_history(self, scope, snap, all_snaps=None, branch=''):
        return await LifeSimPlugin._restore_narrative_history(self, scope, snap, all_snaps, branch)
    async def _apply_rollback(self, session, scope, n):
        return await LifeSimPlugin._apply_rollback(self, session, scope, n)
    async def _load_sim(self, event):
        return await self.sim_store.load(self._sim_session_key(event))
    async def _save_sim(self, event, session):
        await self.sim_store.save(self._sim_session_key(event), session)


async def run_scenario(name, total_turns, real_turns, undo_n, expect_deleted):
    """构造一个"旧版"畸形会话并验证 /undo N 的剧情记录删除数。

    total_turns: lore_turn(含失败轮)
    real_turns: 真实成功轮数(user 消息 + 剧情记录数)
    """
    tmp = tempfile.mkdtemp(prefix="undo_n_")
    try:
        p = _FakePlugin(tmp)
        ev = _Event("g1", "u1")
        scope = p._sim_session_key(ev)
        branch = ""

        ids = []
        for t in range(1, real_turns + 1):
            rid = await p.narrative_store.append(scope, {"narrative": f"第{t}轮剧情"}, branch=branch)
            ids.append(rid)

        messages = []
        lore_snaps, rpg_snaps, narr_snaps = [], [], []
        for t in range(1, total_turns + 1):
            if t <= real_turns:
                messages.append({"role": "user", "content": f"输入{t}"})
                messages.append({"role": "assistant", "content": f"第{t}轮剧情"})
            # 快照在每轮开始前抓取:ids = 之前已完成轮次的记录
            narr_snaps.append({"turn": t, "scope": scope,
                               "ids": ids[: min(t - 1, real_turns)], "revised": []})
            lore_snaps.append({"turn": t, "world_lore": [], "character_lore": {}})
            rpg_snaps.append({"turn": t})

        session = {
            "world_setting": "奇幻世界", "mode": "A", "lore_turn": total_turns,
            "messages": messages,
            "world_lore": [], "character_lore": {},
            "lore_snapshots": lore_snaps,
            "rpg_snapshots": rpg_snaps,
            "narrative_snapshots": narr_snaps,
            "last_narrative_id": ids[-1] if ids else None,
        }
        await p._save_sim(ev, session)

        before = await p.narrative_store.list(scope, branch)
        session = await p._load_sim(ev)
        stats = await p._apply_rollback(session, scope, undo_n)
        assert stats is not None, f"[{name}] 没有可撤销的轮次?"
        assert stats["user_n"] == min(undo_n, real_turns), stats["user_n"]
        after = await p.narrative_store.list(scope, branch)
        deleted = before_count = None
        narr_stats = stats["narr_stats"] or {}
        deleted = narr_stats.get("deleted", 0)
        print(f"[{name}] total={total_turns} real={real_turns} undo={undo_n} "
              f"deleted={deleted} (expect {expect_deleted}) remaining={len(after)}")
        assert deleted == expect_deleted, (
            f"[{name}] BUG: /undo {undo_n} 应删除 {expect_deleted} 条剧情记录,"
            f"实际 {deleted} 条"
        )
        # 剧情记录与消息截断的一致性:剩余记录数 = 剩余真实轮数
        remain_msgs = sum(1 for m in session["messages"] if m.get("role") == "user")
        assert len(after) == max(0, real_turns - undo_n), (
            f"[{name}] 剩余记录 {len(after)} != 剩余轮 {remain_msgs}"
        )
        print(f"[{name}] OK (remaining records={len(after)}, messages={remain_msgs})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def build_session(scope, ids, turn_stamped):
    """构建多轮成功会话(与新版 _generate 提交 turn 后的 session 形态一致)。"""
    total = len(ids)
    messages = []
    lore_snaps, rpg_snaps, narr_snaps = [], [], []
    for t in range(1, total + 1):
        um = {"role": "user", "content": f"输入{t}"}
        if turn_stamped:
            um["turn"] = t
        messages.append(um)
        messages.append({"role": "assistant", "content": f"第{t}轮剧情"})
        narr_snaps.append({"turn": t, "ids": ids[: t - 1], "revised": []})
        lore_snaps.append({"turn": t, "world_lore": [], "character_lore": {}})
        rpg_snaps.append({"turn": t})
    return {
        "world_setting": "奇幻世界", "mode": "A", "lore_turn": total,
        "messages": messages, "world_lore": [], "character_lore": {},
        "lore_snapshots": lore_snaps, "rpg_snapshots": rpg_snaps,
        "narrative_snapshots": narr_snaps, "last_narrative_id": ids[-1],
    }

async def stamp_scenario(name, undo_n, expect_deleted):
    """新会话(消息带 turn 戳)路径。/undo N 精确删 N 条。"""
    tmp = tempfile.mkdtemp(prefix="undo_stamp_")
    try:
        p = _FakePlugin(tmp)
        ev = _Event("g2", "u9")
        scope = p._sim_session_key(ev)
        branch = ""
        ids = []
        for t in range(1, 6):
            rid = await p.narrative_store.append(scope, {"narrative": f"第{t}轮剧情"}, branch=branch)
            ids.append(rid)
        session = build_session(scope, ids, turn_stamped=True)
        await p._save_sim(ev, session)
        session = await p._load_sim(ev)
        stats = await p._apply_rollback(session, scope, undo_n)
        assert stats is not None
        deleted = (stats["narr_stats"] or {}).get("deleted", 0)
        print(f"[stamp::{name}] undo={undo_n} deleted={deleted} (expect {expect_deleted})")
        assert deleted == expect_deleted, f"stamp BUG: deleted {deleted}"
        remain = sum(1 for m in session["messages"] if m.get("role") == "user")
        recs = await p.narrative_store.list(scope, branch)
        assert len(recs) == remain == 5 - undo_n, (len(recs), remain)
        assert session["lore_turn"] == max(1, 5 - undo_n + 1), session["lore_turn"]
        print(f"[stamp::{name}] OK (remaining={len(recs)})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

async def main():
    # 1) 基础情形:5 轮全成功,undo 5 → 删 5
    await run_scenario("ideal", total_turns=5, real_turns=5, undo_n=5, expect_deleted=5)
    # 2) 旧版缺陷:8 个 turn(其中 3 个失败在末尾),5 条消息 → undo 5 应删 5
    await run_scenario("trailing-fails", total_turns=8, real_turns=5, undo_n=5, expect_deleted=5)
    # 3) 失败在最前:3 个 turn(1 失败在开头),2 条消息 → undo 2 应删 2
    await run_scenario("leading-fail", total_turns=3, real_turns=2, undo_n=2, expect_deleted=2)
    # 4) 中间穿插失败:5 turns,3 条消息(第 2、4 轮失败)→ undo 3 应删 3
    await run_scenario("interleaved-fails", total_turns=5, real_turns=3, undo_n=3, expect_deleted=3)
    # 5) undo 1 基础
    await run_scenario("undo1", total_turns=2, real_turns=2, undo_n=1, expect_deleted=1)
    # 6) 新版带 turn 戳会话:undo 5 / undo 3 / undo 1
    await stamp_scenario("stamp5", 5, 5)
    await stamp_scenario("stamp3", 3, 3)
    await stamp_scenario("stamp1", 1, 1)
    print("\nALL UNDO N TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())