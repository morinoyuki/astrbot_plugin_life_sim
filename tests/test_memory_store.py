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

    # 8. 删除管理接口
    # 准备数据
    await store.delete_scope(scope)
    id_a = await store.add(scope, "勇者救下少女", turn=1, importance=3)
    id_b = await store.add(scope, "勇者击败魔王", turn=2, importance=2)
    id_c = await store.add(scope, "商店里买了几瓶药水", turn=3, importance=1)
    assert await store.count(scope) == 3

    # 按 id 删除:删 b
    removed = await store.delete_entries_by_id(scope, [id_b])
    assert removed == 1
    assert await store.count(scope) == 2
    entries = await store.recent(scope, 100)
    ids = {e["id"] for e in entries}
    assert id_b not in ids and id_a in ids and id_c in ids

    # 按关键字删除(模拟 keyword 模式逻辑)
    entries = await store.recent(scope, 100)
    kw = "少女"
    remaining = [e for e in entries if kw not in e["content"]]
    expect_remove = len(entries) - len(remaining)
    await store.replace_entries(scope, remaining)
    assert expect_remove == 1
    after = await store.recent(scope, 100)
    assert len(after) == 1 and "少女" not in after[0]["content"]

    # 清空
    removed = await store.delete_scope(scope)
    assert removed == 1 and await store.count(scope) == 0

    # 9. /undo 回滚:按 turn 删除
    await store.add(scope, "童年发现剑冢", turn=5, importance=2)
    await store.add(scope, "少年拜师学剑", turn=8, importance=2)
    await store.add(scope, "青年夺得比武冠军", turn=12, importance=3)
    assert await store.count(scope) == 3
    # 回滚到 turn=8:删除 turn>8 的记忆(即 turn=12 那条)
    removed = await store.delete_entries_after_turn(scope, 8)
    assert removed == 1
    entries = await store.recent(scope, 100)
    turns = {e.get("turn") for e in entries}
    assert turns == {5, 8}, turns
    # 回滚到 turn=5:再删 turn>5 的(即 turn=8 那条)
    removed = await store.delete_entries_after_turn(scope, 5)
    assert removed == 1
    entries = await store.recent(scope, 100)
    turns = {e.get("turn") for e in entries}
    assert turns == {5}, turns
    # 回滚到 turn=5(无变化)
    removed = await store.delete_entries_after_turn(scope, 5)
    assert removed == 0
    await store.delete_scope(scope)

    # 10. 多步回滚 /undo 4:一次删除连续 4 轮产生的记忆
    # 模拟连续 4 轮成功 /do,每轮各存一条(turn=1,2,3,4)
    for t in range(1, 5):
        await store.add(scope, f"第{t}轮发生的剧情事件内容", turn=t, importance=1)
    # 另加一条手动 memorize(重要度 3,也属 turn 4)
    await store.add(scope, "关键伏笔:某组织的秘密", turn=4, importance=3)
    assert await store.count(scope) == 5
    # undo 4:回滚到 target_turn=1,应删掉 turn>1 的全部 4 条(含 turn4 的两条)
    removed = await store.delete_entries_after_turn(scope, 1)
    assert removed == 4, removed
    entries = await store.recent(scope, 100)
    turns = sorted(e.get("turn") for e in entries)
    assert turns == [1], turns
    await store.delete_scope(scope)

    print("✅ 全部通过")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
