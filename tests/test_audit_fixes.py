"""审计修复回归测试(2026-09 全量复核)。

覆盖:头像精确匹配与删除、Web 关键词删记忆保留向量、聊天卡片默认头像回退、
markdown 图片行本地路径白名单。
可单独运行:`.venv/bin/python tests/test_audit_fixes.py`
"""

import asyncio
import importlib.util
import io
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

from lsim_pkg.avatar_store import AvatarStore
from lsim_pkg.im_render.engine import ChatRenderer
from lsim_pkg.memory_store import MemoryStore
from PIL import Image


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


def test_avatar_exact_match_and_delete():
    """头像名是另一个名字的前缀时,取图/删除必须精确,不能误伤。"""
    with tempfile.TemporaryDirectory() as t:
        store = AvatarStore(t)
        b = _png()
        assert store.save_avatar("小亚", b, "image/png", scope="g1")
        assert store.save_avatar("小亚姐", b, "image/png", scope="g1")
        got = store.get_avatar("小亚", "g1")
        assert got and got.endswith("%E5%B0%8F%E4%BA%9A.png")
        got2 = store.get_avatar("小亚姐", "g1")
        assert got2 and got2.endswith("%E5%B0%8F%E4%BA%9A%E5%A7%90.png")
        assert store.delete("小亚", "g1") is True
        assert store.get_avatar("小亚", "g1") is None
        assert store.get_avatar("小亚姐", "g1") is not None  # 未被误删


def test_memory_keyword_delete_keeps_vectors():
    """按关键词删记忆后,保留的条目仍应带嵌入向量(可被语义检索)。"""

    async def run():
        with tempfile.TemporaryDirectory() as t:
            store = MemoryStore(t)
            await store.add("g1", "我在常青森林抓到了皮卡丘", turn=1)
            await store.add("g1", "小刚的岩石道馆很难打", turn=2)
            assert await store.count("g1") == 2
            raw = await store.raw_entries("g1")
            assert all(e.get("vector") for e in raw)
            removed = await store.delete_entries_by_keyword("g1", "皮卡丘")
            assert removed == 1
            raw = await store.raw_entries("g1")
            assert len(raw) == 1 and raw[0].get("vector")
            hits = await store.search("g1", "道馆", top_k=5)
            assert hits  # 语义检索仍可用

    asyncio.run(run())


def test_default_avatar_fallback():
    """avatars[""] 是全局默认头像,任何未设专属头像的说话人都应回退到它。"""
    r = ChatRenderer()
    r.avatars = {"": "/tmp/default.png", "A": "/tmp/A.png"}
    assert r.resolve_avatar("A") == "/tmp/A.png"
    assert r.resolve_avatar("B") == "/tmp/default.png"
    assert r.resolve_avatar("完全没见过的角色") == "/tmp/default.png"


def test_image_root_guard():
    """markdown 图片行只能读白名单目录下的本地文件。"""
    from lsim_pkg.im_render.rows import _local_image_allowed, register_image_root

    outside = "/etc/passwd"
    if os.path.exists(outside):
        assert _local_image_allowed(outside) is False
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "x.png")
        with open(f, "wb") as fh:
            fh.write(_png())
        assert _local_image_allowed(f) is False  # 未注册目录
        register_image_root(d)
        assert _local_image_allowed(f) is True
        # 穿越尝试不应通过
        assert _local_image_allowed(os.path.join(d, "..", "..", "etc", "passwd")) is False


if __name__ == "__main__":
    test_avatar_exact_match_and_delete()
    test_memory_keyword_delete_keeps_vectors()
    test_default_avatar_fallback()
    test_image_root_guard()
    print("all audit-fix tests passed")
