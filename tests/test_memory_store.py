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

from lsim_pkg.main import (
    LifeSimPlugin,
    _clean_narrative_for_memory,
    _escape_memory_content,
    _strip_meta_tags,
    _strip_xml_tags,
)
from lsim_pkg.memory_store import MemoryStore


def test_compact_turn_summary():
    """自动记忆应为精简摘要而非整段叙事原文,避免过长 + 描述词堆砌。"""
    narr = (
        "阿龙晃了晃药剂瓶。\n"
        "阿龙:放心，交给我。\n"
        "凌霜:那就拜托你了。\n"
        "他们一起离开了城门,往北境而去。\n"
        "月光洒下,晚风轻拂。"
    )
    s = LifeSimPlugin._compact_turn_summary(narr, 200)
    assert len(s) <= 210
    assert "他们一起离开了城门" in s or "阿龙" in s
    # 截断
    long_narr = "描述词 " * 40
    s2 = LifeSimPlugin._compact_turn_summary(long_narr, 60)
    assert len(s2) <= 65


def test_called_memorize():
    """判断本轮 agent loop 是否已调 life_sim_memorize(避免自动记录重复)。"""

    class HooksYes:
        def __init__(self):
            self.steps = [
                {"tool_calls": [{"name": "rpg_get_status"}]},
                {"tool_calls": [{"name": "life_sim_memorize"}]},
            ]

    class HooksNo:
        def __init__(self):
            self.steps = [{"tool_calls": [{"name": "rpg_get_status"}]}]

    assert LifeSimPlugin._called_memorize(HooksYes()) is True
    assert LifeSimPlugin._called_memorize(HooksNo()) is False
    assert LifeSimPlugin._called_memorize(None) is False
    """注入记忆时,内容里的尖括号等非法字符应被转义,避免破坏 <memory_recall> 标签闭合。"""
    assert _escape_memory_content("a<b 且 c>d") == "a〈b 且 c〉d"
    assert _escape_memory_content("残留 </d>") == "残留 〈/d〉"
    assert _escape_memory_content("普通 正常") == "普通 正常"
    assert _escape_memory_content("") == ""
    # 闭环:历史旧记忆含不配对尖括号 → 注入转义 → 包进 memory_recall → 剥离干净
    mem = "伤害公式 a<b 且 c>d,旧数据<龙息>残留"
    esc = _escape_memory_content(mem)
    assert "<" not in esc and ">" not in esc
    block = "<memory_recall>回忆:\n- " + esc + "\n</memory_recall>"
    user = "继续\n" + block
    assert _strip_xml_tags(user) == "继续", _strip_xml_tags(user)
    assert _strip_meta_tags(user) == "继续"


def test_escape_memory_content():
    """注入记忆时,内容里的尖括号等非法字符应被转义,避免破坏 <memory_recall> 标签闭合。"""
    assert _escape_memory_content("a<b 且 c>d") == "a〈b 且 c〉d"
    assert _escape_memory_content("残留 </d>") == "残留 〈/d〉"
    assert _escape_memory_content("普通 正常") == "普通 正常"
    assert _escape_memory_content("") == ""
    # 闭环:历史旧记忆含不配对尖括号 → 注入转义 → 包进 memory_recall → 剥离干净
    mem = "伤害公式 a<b 且 c>d,旧数据<龙息>残留"
    esc = _escape_memory_content(mem)
    assert "<" not in esc and ">" not in esc
    block = "<memory_recall>回忆:\n- " + esc + "\n</memory_recall>"
    user = "继续\n" + block
    assert _strip_xml_tags(user) == "继续", _strip_xml_tags(user)
    assert _strip_meta_tags(user) == "继续"


def test_clean_narrative_strips_tags():
    """记忆内容必须清洗掉聊天卡片标签(<d>/<c>/<t>),避免注入后 _strip_xml_tags 误剥。"""
    narr = (
        "# 深夜的抉择\n\n"
        "<c>阿龙晃了晃药剂瓶。</c>\n\n"
        "<d name=\"阿龙\">放心，交给我。</d>\n"
        "<d name=\"凌霜\" me>那就拜托你了。</d>\n\n"
        "<t>HP 78/100</t><t>🔥 灼烧</t>\n\n"
        "1. 冲上去救她\n2. 撤退\n\n他们一起离开了城门。\n"
    )
    clean = _clean_narrative_for_memory(narr)
    assert "<d" not in clean and "<c" not in clean and "<t" not in clean, clean
    assert "阿龙:放心，交给我。" in clean, clean
    assert "凌霜:那就拜托你了。" in clean, clean
    assert "他们一起离开了城门" in clean, clean

    comment = "我的输入\n<memory_recall>回忆:\n" + clean + "\n</memory_recall>"
    assert _strip_xml_tags(comment) == "我的输入", _strip_xml_tags(comment)
    assert _strip_meta_tags(comment) == "我的输入", _strip_meta_tags(comment)

    assert _clean_narrative_for_memory("   ") == ""
    assert _clean_narrative_for_memory("<d name=\"x\"> </d>") == ""


def test_strip_xml_strips_recall_with_inner_tags():
    """核心修复:记忆里即使残留聊天卡片标签/不成对尖括号,<memory_recall> 也
    必须被 _strip_xml_tags 完整剔除,不残留任何片段(否则污染剧情历史/重放)。"""
    cases = [
        (# 记忆含成对 <d> 标签 —— 旧逻辑会把 <memory_recall> 错配剥坏
         '我的输入\n<memory_recall>回忆:<d name="阿龙">你好</d> 我们离开了</memory_recall>',
         "我的输入"),
        (# 记忆含孤立 </d> 结束标记
         '输入\n<memory_recall>残留 </d> 结束</memory_recall>', "输入"),
        (# 记忆含未闭合 <d 开标签
         '<system_reminder>User: 1</system_reminder>我的输入\n<memory_recall>提到 <d name="x"> 未闭合</memory_recall>',
         "我的输入"),
        (# 正常无内标签
         '我的输入\n<memory_recall>回忆: 普通</memory_recall>', "我的输入"),
        (# 记忆在开头
         '<memory_recall>只有回忆</memory_recall>普通文本', "普通文本"),
    ]
    for raw, expect in cases:
        r = _strip_xml_tags(raw)
        assert r == expect, (raw, r, expect)
        assert "<memory_recall>" not in r and "</memory_recall>" not in r, r
    # 普通环境标签仍被通用剥离
    assert _strip_xml_tags("a<Quoted Message>引用</Quoted Message>b") == "ab"
    assert _strip_xml_tags("你好<system_reminder>x</system_reminder>") == "你好"


def test_recall_tag_isolated():
    """回忆注入用 <memory_recall> XML 标签包裹,下游剥离函数必须能完整剥掉,
    避免污染剧情历史 user_action / 向量记忆『用户行动』/ /redo 重放。"""
    recall = (
        "<memory_recall>以下为与当前剧情相关的往昔回忆:\n"
        "- 用户行动:勇者救下少女 → ...\n"
        "- 用户行动:击败魔王 → ...\n"
        "</memory_recall>"
    )
    user_input = (
        "我继续前进\n"
        + "<system_reminder>User ID: 1</system_reminder>\n"
        + "<narrative_ref>n_abc</narrative_ref>\n"
        + recall
    )
    assert _strip_meta_tags(user_input) == "我继续前进", _strip_meta_tags(user_input)
    assert _strip_xml_tags(user_input) == "我继续前进", _strip_xml_tags(user_input)


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

    # 9. /undo 回滚:按 turn 删除(turn >= target,含 target 轮自身)
    await store.add(scope, "童年发现剑冢", turn=5, importance=2)
    await store.add(scope, "少年拜师学剑", turn=8, importance=2)
    await store.add(scope, "青年夺得比武冠军", turn=12, importance=3)
    assert await store.count(scope) == 3
    # 回滚到 turn=8:删 turn>=8 的记忆(turn 8 与 12 都删)
    removed = await store.delete_entries_from_turn(scope, 8)
    assert removed == 2
    entries = await store.recent(scope, 100)
    turns = {e.get("turn") for e in entries}
    assert turns == {5}, turns
    # 回滚到 turn=5:再删 turn>=5 的(即 turn=5 那条)
    removed = await store.delete_entries_from_turn(scope, 5)
    assert removed == 1
    entries = await store.recent(scope, 100)
    assert entries == []
    # 无可删
    removed = await store.delete_entries_from_turn(scope, 5)
    assert removed == 0
    await store.delete_scope(scope)

    # 10. 多步回滚 /undo 4:一次删除连续 4 轮产生的记忆
    # 模拟连续 4 轮成功 /do,每轮各存一条(turn=1,2,3,4)
    for t in range(1, 5):
        await store.add(scope, f"第{t}轮发生的剧情事件内容", turn=t, importance=1)
    # 另加一条手动 memorize(重要度 3,也属 turn 4)
    await store.add(scope, "关键伏笔:某组织的秘密", turn=4, importance=3)
    assert await store.count(scope) == 5
    # undo 4:回滚到 target_turn=1,应删掉 turn>=1 的所有 5 条(含 turn1-4 全部)
    removed = await store.delete_entries_from_turn(scope, 1)
    assert removed == 5, removed
    assert await store.count(scope) == 0
    await store.delete_scope(scope)

    print("✅ 全部通过")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    # 同步测试(标签剥离)
    test_recall_tag_isolated()
    print("✓ 回忆标签可被剥离")
    test_clean_narrative_strips_tags()
    print("✓ 记忆内容清洗掉聊天卡片标签")
    test_strip_xml_strips_recall_with_inner_tags()
    print("✓ 记忆含内标签时 <memory_recall> 仍被完整清除")
    test_escape_memory_content()
    print("✓ 注入记忆时非法字符被转义")
    test_compact_turn_summary()
    print("✓ 自动记忆存精简摘要(非整段原文)")
    test_called_memorize()
    print("✓ 检测本轮是否已调 life_sim_memorize")
    asyncio.run(main())
