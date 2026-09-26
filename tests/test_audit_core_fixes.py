"""核心审计修复回归测试:

- NarrativeStore.locate:跨主线/分支定位记录 ID
- life_sim_revise_narrative:显式 record_id 在分支里也能修订
- _generate 期间 _load_sim 返回内存权威 session(/redo 回滚不被磁盘旧 lore 覆盖)
- RpgStore.purge_group("", ""):无法判定归属时不做任何删除
- /删除历史:删掉最近一条后清空/回退 last_narrative_id

运行:.venv/bin/python -m pytest tests/test_audit_core_fixes.py -q
"""
import asyncio
import importlib.util
import os
import sys
import tempfile
import time

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

from lsim_pkg.main import LifeSimPlugin
from lsim_pkg.storage_narrative import NarrativeStore
from lsim_pkg.storage_rpg import RpgStore
from lsim_pkg.storage_sim import SimStore


class _Event:
    def __init__(self, gid="g1", uid="u1"):
        self.group_id = gid
        self.message_str = ""
        self.message_obj = type(
            "o", (), {"group_id": gid, "timestamp": time.time()}
        )()

    def get_sender_id(self):
        return "u1"

    def plain_result(self, text):
        return type("R", (), {"text": text})()


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ── 1. locate 跨分支定位 ──────────────────────────────────────────


def test_locate_finds_branch_record():
    tmp = tempfile.mkdtemp(prefix="narr_locate_")
    try:
        store = NarrativeStore(tmp)
        scope = "group_g1"

        async def _go():
            main_id = await store.append(scope, {"narrative": "主线剧情"})
            branch_id = await store.append(
                scope, {"narrative": "分支剧情"}, branch="B1"
            )
            b, rec = await store.locate(scope, branch_id)
            assert b == "B1"
            assert rec is not None and rec["narrative"] == "分支剧情"
            b2, rec2 = await store.locate(scope, main_id)
            assert b2 == "" and rec2 is not None
            b3, rec3 = await store.locate(scope, "n_nonexistent")
            assert b3 == "" and rec3 is None

        _run(_go())
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


# ── 2. 显式 record_id 的分支修订 ──────────────────────────────────


class _RevisePlugin:
    """只绑定 life_sim_revise_narrative 所需成员。"""

    _sim_session_key = LifeSimPlugin._sim_session_key
    _get_sim_lock = LifeSimPlugin._get_sim_lock

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.sim_store = SimStore(data_dir)
        self.narrative_store = NarrativeStore(data_dir)
        self._sim_locks = {}
        self._pending_revise: dict = {}

    async def _load_sim(self, event):
        return await self.sim_store.load(self._sim_session_key(event))

    async def life_sim_revise_narrative(self, event, narrative, record_id=""):
        return await LifeSimPlugin.life_sim_revise_narrative(
            self, event, narrative, record_id
        )


def test_revise_explicit_id_in_branch():
    tmp = tempfile.mkdtemp(prefix="narr_revise_")
    try:
        p = _RevisePlugin(tmp)
        ev = _Event()
        scope = "group_g1"

        async def _go():
            branch_id = await p.narrative_store.append(
                scope, {"narrative": "旧的分支剧情"}, branch="B1"
            )
            await p.sim_store.save(
                scope, {"mode": "A", "current_branch": "B1", "messages": []}
            )
            out = await p.life_sim_revise_narrative(
                ev, "重写后的分支剧情", branch_id
            )
            assert "✅" in out or "已" in out, out
            rec = await p.narrative_store.get(scope, branch_id, branch="B1")
            assert rec["narrative"] == "重写后的分支剧情"

        _run(_go())
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


# ── 3. _generate 期间 _load_sim 用内存权威副本 ────────────────────


class _GenPlugin:
    _sim_session_key = LifeSimPlugin._sim_session_key
    _generate = LifeSimPlugin._generate

    def __init__(self, data_dir):
        self.sim_store = SimStore(data_dir)
        self._active_session: dict = {}
        self._pending_lore: dict = {}
        self._pending_revise: dict = {}

    async def _load_sim(self, event):
        return await LifeSimPlugin._load_sim(self, event)


def test_generate_uses_active_session_and_cleans_up():
    tmp = tempfile.mkdtemp(prefix="gen_active_")
    try:
        p = _GenPlugin(tmp)
        ev = _Event()
        scope = "group_g1"
        disk_session = {"mode": "A", "world_lore": [{"seq": 1, "content": "旧"}]}
        _run(p.sim_store.save(scope, disk_session))

        # 内存会话是 /redo 回滚后的版本(lore 已清空)
        mem_session = {"mode": "A", "world_lore": []}
        seen = {}

        async def fake_inner(event, session, user_input, mode, imgs):
            seen["loaded"] = await p._load_sim(event)
            return "✅ok"

        p._generate_inner = fake_inner
        out = _run(p._generate(ev, mem_session, "继续", "A", None))
        assert out == "✅ok"
        assert seen["loaded"] is mem_session
        # 结束后必须清理,避免残留影响其他命令的 _load_sim
        assert p._active_session == {}

        # 非生成期间仍读磁盘
        assert _run(p._load_sim(ev)) == disk_session
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


# ── 4. purge_group 空 ID 保护 ─────────────────────────────────────


def test_purge_group_empty_ids_is_noop():
    tmp = tempfile.mkdtemp(prefix="rpg_purge_")
    try:
        store = RpgStore(tmp)
        # 造两个私聊角色存档
        for uid in ("u1", "u2"):
            path = store._char_path(uid)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("{}")
        res = store.purge_group("", "")
        assert res == {"deleted_chars": 0, "deleted_sessions": []}
        # 两个存档都还在
        assert os.path.exists(store._char_path("u1"))
        assert os.path.exists(store._char_path("u2"))
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


# ── 5. /删除历史 清理 last_narrative_id ───────────────────────────


class _DelHistPlugin:
    _sim_session_key = LifeSimPlugin._sim_session_key
    _extract_after_cmd = LifeSimPlugin._extract_after_cmd
    _get_sim_lock = LifeSimPlugin._get_sim_lock
    _busy_message = LifeSimPlugin._busy_message

    def __init__(self, data_dir):
        self.sim_store = SimStore(data_dir)
        self.narrative_store = NarrativeStore(data_dir)
        self._sim_locks = {}

    async def _load_sim(self, event):
        return await self.sim_store.load(self._sim_session_key(event))

    async def _save_sim(self, event, session):
        await self.sim_store.save(self._sim_session_key(event), session)

    async def cmd_delete_history(self, event):
        gen = LifeSimPlugin.cmd_delete_history(self, event)
        return [r.text async for r in gen]


def test_delete_history_clears_stale_last_id():
    tmp = tempfile.mkdtemp(prefix="hist_del_")
    try:
        p = _DelHistPlugin(tmp)
        ev = _Event()
        ev.message_str = "/删除历史 n_last"
        scope = "group_g1"

        async def _go():
            keep = await p.narrative_store.append(scope, {"narrative": "第一条"})
            last = await p.narrative_store.append(scope, {"narrative": "第二条"})
            await p.sim_store.save(
                scope,
                {
                    "mode": "A",
                    "messages": [],
                    "last_narrative_id": last,
                },
            )
            ev.message_str = f"/删除历史 {last}"
            texts = await p.cmd_delete_history(ev)
            assert any("已删除" in t for t in texts)
            s = await p.sim_store.load(scope)
            # 回退到剩余记录里最新的一条
            assert s["last_narrative_id"] == keep

            # 删最后一条后清空
            ev.message_str = f"/删除历史 {keep}"
            await p.cmd_delete_history(ev)
            s = await p.sim_store.load(scope)
            assert not s.get("last_narrative_id")

        _run(_go())
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
