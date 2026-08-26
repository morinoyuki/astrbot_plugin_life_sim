"""历史压缩「按用户输入轮数保留」逻辑测试。

运行方式(在插件根目录):
    .venv/bin/python -m pytest tests/test_history_compress.py
"""
import asyncio
import importlib.util
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

from lsim_pkg.main import LifeSimPlugin  # 需在 sys.path 注入后导入

CFG = {
    "max_history_chars": 60000,
    "keep_tail_messages": 2,
    "use_llm_compress": False,  # 测试走规则摘要路径
}


def make_plugin(cfg=None):
    p = object.__new__(LifeSimPlugin)
    p._cfg = lambda key, default=None: (cfg or CFG).get(key, default)
    return p


def run(coro):
    return asyncio.run(coro)


def U(n, text="输入"):
    return {"role": "user", "content": f"{text}{n}"}


def A(n, text="回复"):
    return {"role": "assistant", "content": f"{text}{n}"}


def T(n):
    return {"role": "tool", "tool_call_id": f"c{n}", "content": f"工具结果{n}"}


def SUM(text):
    return {"role": "user", "_summary": True, "content": text}


# ── 切分函数 ────────────────────────────────────────────────────

def test_split_basic_turn_boundary():
    """切分边界落在真实用户消息上:该轮的 tool/assistant 随属同轮。"""
    msgs = [U(1), T(11), A(1), U(2), T(22), A(2), U(3), A(3)]
    head, tail = LifeSimPlugin._split_tail_by_turns(msgs, 2)
    assert head == msgs[:3]           # 第 1 轮(u1+工具+回复)被压缩
    assert tail == msgs[3:]           # 第 2/3 轮完整保留(5 条)


def test_split_summary_marker_not_a_turn():
    """旧压缩产物 `_summary` 不算用户输入,归入 head。"""
    msgs = [SUM("旧摘要"), U(1), A(1), U(2), A(2)]
    head, tail = LifeSimPlugin._split_tail_by_turns(msgs, 1)
    assert head == msgs[:3]           # 摘要 + 第 1 轮进 head
    assert tail == msgs[3:]           # 只留最后 1 轮


def test_split_fewer_turns_keeps_all():
    """轮数不足 keep_tail → 全部保留(head 空)。"""
    msgs = [U(1), A(1), U(2), A(2)]
    head, tail = LifeSimPlugin._split_tail_by_turns(msgs, 10)
    assert head == [] and tail == msgs


def test_split_keep_turns_clamped_to_one():
    """keep_turns ≤ 0 视为至少保 1 轮。"""
    msgs = [U(1), A(1), U(2), A(2)]
    head, tail = LifeSimPlugin._split_tail_by_turns(msgs, 0)
    assert head == msgs[:2] and tail == msgs[2:]


def test_split_no_real_user_message():
    """没有任何真实用户输入(只有摘要/非 dict)→ 全部保留。"""
    msgs = [SUM("摘要"), {"role": "assistant", "content": "x"}]
    head, tail = LifeSimPlugin._split_tail_by_turns(msgs, 2)
    assert head == [] and tail == msgs


def test_split_assistant_before_first_user():
    """开头孤立的非 user 消息归入 head(不产生轮)。"""
    msgs = [{"role": "system-ish", "content": "杂项"}, U(1), A(1)]
    head, tail = LifeSimPlugin._split_tail_by_turns(msgs, 5)
    assert head == [] and tail == msgs


# ── 压缩主流程 ──────────────────────────────────────────────────

def _long_history(turns: int) -> list:
    """构造 turns 轮、每轮正文足够长的消息列表。"""
    filler = "剧情内容" * 500  # 2000 字/条
    msgs = []
    for i in range(turns):
        msgs.append({"role": "user", "content": f"[第{i}轮行动]{filler}"})
        if i % 2 == 0:
            msgs.append(T(i))
        msgs.append({"role": "assistant", "content": f"[第{i}轮叙事]{filler}"})
    return msgs


def test_compress_splits_on_turns_not_messages():
    """6 轮历史、保留 2 轮:tail 必须恰好是最后 2 轮(而非固定条数)。"""
    p = make_plugin({"max_history_chars": 1000, "keep_tail_messages": 2,
                     "use_llm_compress": False})
    msgs = _long_history(6)
    out = run(p._compress_history(msgs))

    assert out[0].get("_summary") is True
    # 尾部 = 第 4/5 轮(每轮 user(+tool)+assistant):边界在真实用户消息上
    assert out[1]["role"] == "user" and "第4轮行动" in out[1]["content"]
    assert out[-1]["role"] == "assistant" and "第5轮叙事" in out[-1]["content"]
    # 真实 user 恰好是最后两轮的输入,边界落在用户消息上
    real_users = [m for m in out[1:] if m.get("role") == "user"]
    assert [m["content"][:8] for m in real_users] == ["[第4轮行动]剧", "[第5轮行动]剧"]
    assert all(m["content"].startswith(("[第4轮行动]", "[第5轮行动]")) for m in real_users)


def test_compress_skipped_when_chars_under_limit():
    """总长未超限 → 原样返回(即使轮数 > keep_tail)。"""
    p = make_plugin({"max_history_chars": 60000, "keep_tail_messages": 1,
                     "use_llm_compress": False})
    msgs = [U(1), A(1), U(2), A(2), U(3), A(3)]
    assert run(p._compress_history(msgs)) is msgs


def test_compress_skipped_when_turns_within_limit():
    """轮数未超限但总长超限 → 不强行压缩(尾部已覆盖全部轮次)。"""
    p = make_plugin({"max_history_chars": 100, "keep_tail_messages": 50,
                     "use_llm_compress": False})
    msgs = _long_history(3)
    assert run(p._compress_history(msgs)) is msgs


def test_compress_summary_contains_world_setting():
    """规则摘要应从 head 的首条用户消息提取世界观片段。"""
    world = "[世界观]魔法世界,月亮提供魔力。"
    msgs = [{"role": "user", "content": world}]
    for i in range(4):
        msgs.append({"role": "user", "content": f"行动{i}。" + "长" * 800})
        msgs.append({"role": "assistant", "content": "叙事" * 400})

    p = make_plugin({"max_history_chars": 1000, "keep_tail_messages": 1,
                     "use_llm_compress": False})
    out = run(p._compress_history(msgs))

    assert out[0].get("_summary") is True
    assert "魔法世界" in out[0]["content"]          # 世界观进了摘要
    assert out[1]["role"] == "user"                 # tail 边界 = 最后一条真实用户输入
    assert out[1]["content"].startswith("行动3")


def test_recompress_replaces_old_summary():
    """再次压缩时,旧 `_summary` 归入 head 参与重新生成,不重复堆积。"""
    old_summary = SUM("[叙事历史摘要]早期剧情……")
    msgs = [old_summary]
    for i in range(5):
        msgs.append(U(i))
        msgs.append(A(i))

    p = make_plugin({"max_history_chars": 10, "keep_tail_messages": 2,
                     "use_llm_compress": False})
    out = run(p._compress_history(msgs))

    summaries = [m for m in out if m.get("_summary")]
    assert len(summaries) == 1                      # 旧摘要被替换而非叠加
    assert summaries[0] is not old_summary
    # 旧摘要文本应作为 head 内容参与规则抽取(世界观段来自首条 user 消息)
    assert "早期剧情" in summaries[0]["content"] or "世界观设定" in summaries[0]["content"]
