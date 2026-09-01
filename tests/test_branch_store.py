"""剧情分支存储单元测试。

运行方式(在插件根目录):
    .venv/bin/python tests/test_branch_store.py
"""
import asyncio
import importlib.util
import os
import shutil
import sys
import tempfile
import time

# 把插件根目录注册为包,让 storage_* 模块的相对导入生效
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
_spec = importlib.util.spec_from_file_location(
    "lsim_pkg", os.path.join(_ROOT, "__init__.py"),
    submodule_search_locations=[_ROOT],
)
_pkg = importlib.util.module_from_spec(_spec)
sys.modules["lsim_pkg"] = _pkg
_spec.loader.exec_module(_pkg)

from lsim_pkg.storage_branch import BranchStore, _encode_name, _decode_name
from lsim_pkg.storage_narrative import NarrativeStore
from lsim_pkg.storage_sim import SimStore


class _Event:
    def __init__(self, gid, sid):
        self.group_id = gid
        self.sender_id = sid
        self.message_obj = type("o", (), {"group_id": gid, "timestamp": time.time()})()

    def get_sender_id(self):
        return self.sender_id


class _Mini:
    """只承载分支方法所需的属性/方法的轻量替身(不依赖 AstrBot Context)。"""

    def __init__(self, data_dir):
        self.sim_store = SimStore(data_dir)
        self.narrative_store = NarrativeStore(data_dir)
        self.branch_store = BranchStore(data_dir)

    def _sim_session_key(self, event):
        return f"group_{event.group_id}" if event.group_id else f"user_{event.sender_id}"

    async def _load_sim(self, event):
        return await self.sim_store.load(self._sim_session_key(event))

    async def _save_sim(self, event, session):
        await self.sim_store.save(self._sim_session_key(event), session)


async def _test_store(tmp):
    store = BranchStore(tmp)
    scope = "group_12345"
    for name in ["TE线", "BE线", "a/b\\c:d*e", "😀主线", "主线"]:
        await store.save(scope, name, {"lore_turn": 3, "messages": ["m"]})
    await store.save(scope, "TE线", {"lore_turn": 5, "messages": ["new"]})
    t = await store.get(scope, "TE线")
    assert t["lore_turn"] == 5 and t["name"] == "TE线" and t["saved_at"]
    all_b = await store.list(scope)
    assert set(all_b) == {"TE线", "BE线", "a/b\\c:d*e", "😀主线", "主线"}
    assert await store.delete(scope, "BE线")
    assert "BE线" not in await store.list(scope)
    assert await store.scope_exists(scope)
    assert await store.delete_scope(scope) == 4
    assert not await store.scope_exists(scope)
    assert await store.list(scope) == {}
    # 分支名编解码
    for n in ["TE线", "a/b\\c:d*e", "😀主线"]:
        assert _decode_name(_encode_name(n)) == n
    print("store OK")


async def _test_clear_and_migration(tmp):
    p = _Mini(tmp)
    ev = _Event("g1", "u1")
    scope = p._sim_session_key(ev)
    # 模拟会话 + 分支
    await p._save_sim(ev, {"world_setting": "w", "mode": "A", "messages": []})
    await p.branch_store.save(scope, "分支A", {"lore_turn": 1})
    await p.branch_store.save(scope, "分支B", {"lore_turn": 2})
    assert await p.branch_store.scope_exists(scope)
    # 模拟 /创建 的 _clear_sim
    await p.sim_store.delete(scope)
    n = await p.branch_store.delete_scope(scope)
    assert n == 2
    assert not await p.branch_store.scope_exists(scope)
    # 迁移旧数据
    legacy = {"world_setting": "w", "mode": "A", "branches": {"旧1": {"lore_turn": 9}}}
    await p._save_sim(ev, legacy)
    loaded = await p._load_sim(ev)
    leg = loaded.pop("branches", None)
    assert isinstance(leg, dict)
    await p.branch_store.save(scope, list(leg)[0], leg[list(leg)[0]])
    await p._save_sim(ev, loaded)
    assert set(await p.branch_store.list(scope)) == {"旧1"}
    assert "branches" not in await p._load_sim(ev)
    print("clear/migration OK")


async def main():
    tmp = tempfile.mkdtemp(prefix="lsim_test_")
    try:
        await _test_store(tmp)
        await _test_clear_and_migration(tmp)
        print("ALL BRANCH STORE TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
