"""向量记忆存储测试:本地嵌入回退、写入去重、语义召回、生命周期清理。

运行方式(在插件根目录):
    .venv/bin/python tests/test_memory_store.py
"""
import asyncio
import os
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "lsim_pkg", os.path.join(_ROOT, "__init__.py"),
    submodule_search_locations=[_ROOT],
)
_pkg = importlib.util.module_from_spec(_spec)
sys.modules["lsim_pkg"] = _pkg
_spec.loader.exec_module(_pkg)

from lsim_pkg.memory_store import MemoryStore


def approx(a, b, tol=1e-6):
    return abs(a - b) < tol


async def main():
    tmp = tempfile.mkdtemp(prefix="lsim_mem_test_")
    store = MemoryStore(tmp)
    scope = "group_123"

    # 本地嵌入源(无 provider 时回退)
    assert store.embed_source == "local", store.embed_source

    # 1. 写入与召回:同义片段应命中
    m1 = await store.add(scope, "主角在山谷里救下一名受伤的白发少女,她自称来自北境", turn=1)
    m2 = await store.add(scope, "主角接受国王的册封,成为骑士领地的领主", turn=2)
    assert m1 and m2
    assert await store.count(scope) == 2

    hits = await store.search(scope, "主角救的少女是谁")
    assert hits, "应召回相关记忆"
    assert "少女" in hits[0]["content"], hits[0]["content"]
    assert "score" in hits[0]

    # 2. 无关查询:可能召回或空,但不能抛错
    await store.search(scope, "风马牛不相及的问题")

    # 3. 去重:高度相似内容不再新增
    n1 = await store.count(scope)
    await store.add(scope, "主角在山谷里救下一名受伤的白发少女,她自称来自北境", turn=3)
    n2 = await store.count(scope)
    assert n1 == n2, f"去重失败: {n1} -> {n2}"

    # 4. 空/过短内容丢弃
    assert await store.add(scope, "   ") is None
    assert await store.add(scope, "啊") is None

    # 5. 生命周期:delete_scope 清空并删文件
    n = await store.count(scope)
    assert n > 0
    deleted = await store.delete_scope(scope)
    assert deleted == n, (deleted, n)
    assert await store.count(scope) == 0
    # 再次写入应从头开始(文件已删)
    await store.add(scope, "重新开局", turn=1)
    assert await store.count(scope) == 1
    await store.delete_scope(scope)

    # 6. set_max_entries 裁剪最旧
    topics = ["北境家族的血仇", "主角的剑术老师", "皇城比武大会", "海边小镇的幽灵船", "沙漠里的古代遗迹", "主角的童年玩伴", "修道院的密辛", "边境的兽潮来袭", "贵族的联姻阴谋", "深山的龙族长老"]
    for i in range(10):
        await store.add(scope, f"事件 {i}:{topics[i]},详细描述不同内容避免去重", turn=i)
    assert await store.count(scope) == 10
    trimmed = await store.set_max_entries(scope, 5)
    assert trimmed == 5
    assert await store.count(scope) == 5
    # 最旧的 5 条被丢:编号 0-4 已不在
    recent = await store.recent(scope, 20)
    contents = [r["content"] for r in recent]
    assert not any("事件 0:" in c for c in contents), contents
    assert any("事件 9:" in c for c in contents), contents
    await store.delete_scope(scope)

    # 7. provider 回退:模拟 provider 失败 → 仍可用本地嵌入
    class _BadProvider:
        async def get_embeddings(self, texts):
            raise RuntimeError("provider down")

        def get_dim(self):
            return 512

    store.set_embedding_provider(_BadProvider())
    assert store.embed_source == "provider"
    mid = await store.add(scope, "provider 故障后回退本地", turn=1)
    assert mid is not None
    h = await store.search(scope, "provider 故障回退")
    assert h
    await store.delete_scope(scope)

    print("✅ 全部通过")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
