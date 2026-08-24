"""/redo(重试)逻辑测试:输入提取、回滚、重新生成。

运行方式(在插件根目录):
    .venv/bin/python tests/test_redo.py
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

from lsim_pkg.main import (
    LifeSimPlugin,
    _strip_meta_tags,
    _restore_images_from_content,
    _content_to_text,
)
from lsim_pkg.storage_narrative import NarrativeStore
from lsim_pkg.storage_sim import SimStore


class _Event:
    def __init__(self, gid, sid):
        self.group_id = gid
        self.sender_id = sid
        self.message_obj = type("o", (), {"group_id": gid, "timestamp": time.time()})()

    def get_sender_id(self):
        return self.sender_id

    def get_sender_name(self):
        return "Tester"

    def plain_result(self, text):
        return type("R", (), {"text": text})()


class _FakePlugin:
    """只实现 redo/rollback 路径所需方法;LLM 生成用 mock。"""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.sim_store = SimStore(data_dir)
        self.narrative_store = NarrativeStore(data_dir)
        self.generated = []

    # ── 借 LifeSimPlugin 的实现 ──
    def _sim_session_key(self, event):
        return f"group_{event.group_id}" if event.group_id else f"user_{event.sender_id}"

    def _rpg_restore(self, snapshot):
        return {"restored_chars": 0, "restored_sessions": 0,
                "deleted_chars": 0, "deleted_sessions": 0}

    async def _restore_narrative_history(self, scope, snap, all_snaps=None, branch=''):
        return await LifeSimPlugin._restore_narrative_history(self, scope, snap, all_snaps, branch)

    async def _apply_rollback(self, session, scope, n):
        return await LifeSimPlugin._apply_rollback(self, session, scope, n)

    async def _load_sim(self, event):
        return await self.sim_store.load(self._sim_session_key(event))

    async def _save_sim(self, event, session):
        await self.sim_store.save(self._sim_session_key(event), session)

    async def _generate(self, event, session, user_input, mode, imgs):
        self.generated.append((user_input, mode, imgs))
        return f"🔄 重新生成结果,输入={user_input[:20]!r}"


def _make_session(scope):
    """两轮历史:turn1 与 turn2(第二轮带 system_reminder / narrative_ref 标签)。"""
    r1 = {"id": "n_11111111", "scope": scope, "narrative": "第一轮剧情",
          "user_action": "输入1", "created_at": "2026-01-01T00:00:01+0800",
          "revised_at": "2026-01-01T00:00:01+0800", "revised_count": 0}
    r2 = {"id": "n_22222222", "scope": scope, "narrative": "第二轮剧情(有问题)",
          "user_action": "输入2", "created_at": "2026-01-01T00:00:02+0800",
          "revised_at": "2026-01-01T00:00:02+0800", "revised_count": 0}
    user2 = (
        "我选择去森林深处"
        "<system_reminder>User ID: u1, Nickname: Tester</system_reminder>"
        "<narrative_ref>最近剧情ID: `n_11111111` ...</narrative_ref>"
    )
    return {
        "world_setting": "奇幻世界", "mode": "A", "lore_turn": 2,
        "messages": [
            {"role": "user", "content": "输入1"},
            {"role": "assistant", "content": "第一轮剧情"},
            {"role": "user", "content": [{"type": "text", "text": user2},
                                          {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]},
            {"role": "assistant", "content": "第二轮剧情(有问题)"},
        ],
        "world_lore": [{"seq": 1, "section": "地理", "content": "有森林"}],
        "character_lore": {"主角": [{"seq": 1, "section": "性格", "content": "勇敢"}]},
        "lore_snapshots": [
            {"turn": 1, "world_lore": [], "character_lore": {}},
            {"turn": 2, "world_lore": [{"seq": 1, "section": "地理", "content": "有森林"}],
             "character_lore": {"主角": [{"seq": 1, "section": "性格", "content": "勇敢"}]}},
        ],
        "rpg_snapshots": [{"turn": 1}, {"turn": 2}],
        "narrative_snapshots": [
            {"turn": 1, "scope": scope, "ids": [], "revised": []},
            {"turn": 2, "scope": scope, "ids": ["n_11111111"], "revised": []},
        ],
        "last_narrative_id": "n_22222222",
    }


async def test_strip_meta():
    text = (
        "我去森林"
        "<system_reminder>User ID: u1, Nickname: T</system_reminder>"
        "<narrative_ref>最近剧情ID: `n_1` ...</narrative_ref>"
        "<Quoted Message>\n引用的消息内容\n</Quoted Message>"
    )
    out = _strip_meta_tags(text)
    assert "system_reminder" not in out and "narrative_ref" not in out, out
    assert "Quoted Message" in out, "应保留引用消息"
    assert "我去森林" in out and "引用的消息内容" in out, out
    print("strip_meta OK")


async def test_restore_images():
    content = [
        {"type": "text", "text": "看图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
        {"type": "image_url", "image_url": {"url": "http://example.com/x.png"}},  # 非 data,跳过
    ]
    imgs = _restore_images_from_content(content)
    assert len(imgs) == 1
    assert imgs[0].url == "data:image/png;base64,QUJD"
    assert imgs[0].file == "base64://QUJD"
    assert _restore_images_from_content([]) == []
    print("restore_images OK")


async def test_redo_flow():
    tmp = tempfile.mkdtemp(prefix="redo_test_")
    try:
        p = _FakePlugin(tmp)
        ev = _Event("g1", "u1")
        scope = p._sim_session_key(ev)
        session = _make_session(scope)
        # 预置 narrative 历史
        await p.narrative_store.append(scope, {"narrative": "第一轮剧情"})
        # 手动写入两轮记录(用固定 id 覆盖)
        from lsim_pkg.storage_base import write_json_atomic
        from lsim_pkg.storage_narrative import _gen_id
        d = os.path.join(tmp, "narrative_history", scope)
        os.makedirs(d, exist_ok=True)
        import json as _json
        r1 = session["narrative_snapshots"][0]
        # 重新构造真实记录文件
        with open(os.path.join(d, "n_11111111.json"), "w", encoding="utf-8") as f:
            _json.dump({"id": "n_11111111", "scope": scope, "narrative": "第一轮剧情",
                        "created_at": "2026-01-01T00:00:01+0800",
                        "revised_at": "2026-01-01T00:00:01+0800", "revised_count": 0}, f, ensure_ascii=False)
        with open(os.path.join(d, "n_22222222.json"), "w", encoding="utf-8") as f:
            _json.dump({"id": "n_22222222", "scope": scope, "narrative": "第二轮剧情",
                        "created_at": "2026-01-01T00:00:02+0800",
                        "revised_at": "2026-01-01T00:00:02+0800", "revised_count": 0}, f, ensure_ascii=False)
        await p._save_sim(ev, session)

        # 模拟 _cmd_redo_body 核心流程
        session = await p._load_sim(ev)
        messages = session.get("messages", [])
        last_user = next((m for m in reversed(messages)
                          if m.get("role") == "user" and not m.get("_summary")), None)
        assert last_user is not None
        content = last_user.get("content")
        user_input = _strip_meta_tags(_content_to_text(content))
        assert "系统" not in user_input and "narrative_ref" not in user_input
        assert "我选择去森林深处" in user_input
        assert "n_11111111" not in user_input  # 剧情ID 已剥掉
        imgs = _restore_images_from_content(content)
        assert len(imgs) == 1

        stats = await p._apply_rollback(session, scope, 1)
        assert stats is not None
        assert stats["turns"] == 1
        # 消息截断到 turn1
        msgs = session.get("messages", [])
        assert len(msgs) == 2 and msgs[0]["role"] == "user" and msgs[1]["role"] == "assistant", msgs
        # undo 1 恢复到"上一轮开始前"状态:lore_turn 保持 2,快照 [1,2] 保留
        assert session["lore_turn"] == 2, session["lore_turn"]
        assert [s["turn"] for s in session["lore_snapshots"]] == [1, 2]
        assert [s["turn"] for s in session["rpg_snapshots"]] == [1, 2]
        assert [s["turn"] for s in session["narrative_snapshots"]] == [1, 2]
        # 剧情历史回滚:只剩 r1
        records = await p.narrative_store.list(scope)
        assert [r["id"] for r in records] == ["n_11111111"], records
        # last_narrative_id 修正
        assert session["last_narrative_id"] == "n_11111111", session["last_narrative_id"]

        # 重新生成(mock)
        result = await p._generate(ev, session, user_input, session.get("mode", "A"), imgs)
        assert result.startswith("🔄")
        assert p.generated and p.generated[0][0] == user_input
        assert p.generated[0][2] and len(p.generated[0][2]) == 1
        print("redo flow OK")

        # undo 回滚 + _generate mock 均未落盘,磁盘上仍为原始(4 条消息)
        session2 = await p._load_sim(ev)
        assert len(session2.get("messages", [])) == 4
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def main():
    await test_strip_meta()
    await test_restore_images()
    await test_redo_flow()
    print("ALL REDO TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
