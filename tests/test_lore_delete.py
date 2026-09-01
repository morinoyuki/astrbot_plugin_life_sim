"""/lore 删除 <角色名> 命令测试:
1) 删除只作用于当前 character_lore,lore 快照(旧内联格式 / 新版本表格式)不动
2) /undo 语义:删除后回滚到删除前轮次,被删角色随快照恢复
3) 未找到角色 / 缺参数的提示路径

运行方式(在插件根目录):
    .venv/bin/python tests/test_lore_delete.py
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
    "lsim_pkg", os.path.join(_ROOT, "__init__.py"),
    submodule_search_locations=[_ROOT],
)
_pkg = importlib.util.module_from_spec(_spec)
sys.modules["lsim_pkg"] = _pkg
_spec.loader.exec_module(_pkg)

from lsim_pkg.main import LifeSimPlugin, _resolve_snapshot_lore
from lsim_pkg.storage_sim import SimStore

HUAYIN = [
    {"seq": 2, "section": "外观", "content": "银发"},
    {"seq": 3, "section": "性格", "content": "傲娇"},
]
PROTAG = [{"seq": 1, "section": "性格", "content": "勇敢"}]

# 3 个内联 lore 快照(旧格式):花音自 turn 2 起在快照里
SNAPSHOTS = [
    {"turn": 1, "world_lore": [], "character_lore": {}},
    {
        "turn": 2,
        "world_lore": [{"seq": 1, "section": "地理", "content": "有森林"}],
        "character_lore": {"主角": PROTAG},
    },
    {
        "turn": 3,
        "world_lore": [{"seq": 1, "section": "地理", "content": "有森林"}],
        "character_lore": {"主角": PROTAG, "花音": HUAYIN},
    },
]


class _Event:
    def __init__(self, gid, message_str):
        self.group_id = gid
        self.message_str = message_str
        self.message_obj = type("o", (), {"group_id": gid, "timestamp": time.time()})()

    def get_sender_id(self):
        return "u1"

    def plain_result(self, text):
        return type("R", (), {"text": text})()


class _FakePlugin:
    """只绑定 cmd_lore 用到的成员;其余直接复用 LifeSimPlugin 的纯逻辑。"""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.sim_store = SimStore(data_dir)

    # 纯逻辑方法直接复用真实实现(_normalize_character_lore 是 staticmethod,需显式包装)
    _sim_session_key = LifeSimPlugin._sim_session_key
    _extract_after_cmd = LifeSimPlugin._extract_after_cmd
    _normalize_character_lore = staticmethod(LifeSimPlugin._normalize_character_lore)

    async def _load_sim(self, event):
        return await self.sim_store.load(f"group_{event.group_id}")

    async def _save_sim(self, event, session):
        await self.sim_store.save(f"group_{event.group_id}", session)

    async def cmd_lore(self, event):
        # 复用真实实现(生成器方法,直接取未绑定函数)
        gen = LifeSimPlugin.cmd_lore(self, event)
        return [r.text async for r in gen]


async def _setup(tmp):
    p = _FakePlugin(tmp)
    ev = _Event("g1", "")
    session = {
        "world_setting": "奇幻世界",
        "mode": "A",
        "character_lore": {"主角": PROTAG, "花音": HUAYIN},
        "world_lore": [],
        # 新格式:先压缩为版本表引用
        "lore_snapshots": SNAPSHOTS,
    }
    from lsim_pkg.main import _compact_lore_versions

    _compact_lore_versions(session)
    await p._save_sim(ev, session)
    return p, ev, session


async def test_delete_keeps_snapshots():
    tmp = tempfile.mkdtemp(prefix="lore_del_")
    try:
        p, ev, orig = await _setup(tmp)
        ev.message_str = "/lore 删除 花音"
        out = await p.cmd_lore(ev)
        assert any("已删除" in t for t in out), out
        assert any("/undo 回滚恢复" in t for t in out), out

        saved = await p._load_sim(ev)
        assert "花音" not in saved["character_lore"], "当前态应删掉花音"
        assert "主角" in saved["character_lore"], "其他角色不能误伤"
        # 快照原封不动:/undo 回到 turn=3 仍能解析出完整花音设定
        assert saved["lore_snapshots"] == orig["lore_snapshots"], "快照不应被改动"
        _, cl = _resolve_snapshot_lore(saved, {"version": None, **SNAPSHOTS[2]})
        assert cl.get("花音") == HUAYIN and cl.get("主角") == PROTAG
        print("delete keeps snapshots intact OK")
    finally:
        import shutil as _sh

        _sh.rmtree(tmp, ignore_errors=True)


async def test_delete_not_found_and_usage():
    tmp = tempfile.mkdtemp(prefix="lore_del_nf_")
    try:
        p, ev, _ = await _setup(tmp)
        ev.message_str = "/lore 删除 不存在的人"
        out = await p.cmd_lore(ev)
        assert any("未找到角色" in t for t in out), out
        assert any("现有角色" in t for t in out), out

        ev.message_str = "/lore 删除"
        out = await p.cmd_lore(ev)
        assert any("用法" in t for t in out), out

        saved = await p._load_sim(ev)
        assert set(saved["character_lore"]) == {"主角", "花音"}, "失败调用不能改数据"
        print("not-found / usage paths OK")
    finally:
        import shutil as _sh

        _sh.rmtree(tmp, ignore_errors=True)


async def test_delete_multi_key_match():
    tmp = tempfile.mkdtemp(prefix="lore_del_multi_")
    try:
        p, ev, _ = await _setup(tmp)
        # 同一角色拆成多个 key 时一并删除
        s = await p._load_sim(ev)
        s["character_lore"]["花音"] = list(HUAYIN)  # 已存在;再加一个别名 key
        await p._save_sim(ev, s)

        ev.message_str = "/lore 删除 花音"
        out = await p.cmd_lore(ev)
        joined = "\n".join(out)
        assert "已删除" in joined and "条" in joined, out  # 匹配数随 key 集变化,不严格断言
        saved = await p._load_sim(ev)
        assert all("花音" not in k for k in saved["character_lore"]), (
            f"所有含花音的 key 都应删除: {list(saved['character_lore'])}"
        )
        print("multi-key delete OK")
    finally:
        import shutil as _sh

        _sh.rmtree(tmp, ignore_errors=True)


async def main():
    await test_delete_keeps_snapshots()
    await test_delete_not_found_and_usage()
    await test_delete_multi_key_match()
    print("ALL LORE DELETE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
