"""插件页面(WebUI 数据管理)REST 接口测试。

不启动 dashboard,直接绑定调用 handler;_web_request 用 stub 替身。

运行方式(在插件根目录):
    .venv/bin/python -m pytest tests/test_plugin_page.py
"""

import asyncio
import importlib.util
import json
import os
import sys
from types import SimpleNamespace as NS

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
_spec = importlib.util.spec_from_file_location(
    "lsim_pkg", os.path.join(_ROOT, "__init__.py"),
    submodule_search_locations=[_ROOT],
)
_pkg = importlib.util.module_from_spec(_spec)
sys.modules["lsim_pkg"] = _pkg
_spec.loader.exec_module(_pkg)

import lsim_pkg.main as m
import pytest
from lsim_pkg.storage_base import write_json_atomic


class FakeQuery:
    def __init__(self, kv: dict | None = None):
        self._kv = kv or {}

    def get(self, name, default=None):
        return self._kv.get(name, default)


class FakeWebRequest:
    """替身 astrbot.api.web.request 代理。"""

    def __init__(self, body=None, query=None):
        self._body = body
        self.query = FakeQuery(query)

    async def json(self, default=None):
        return self._body if self._body is not None else default


@pytest.fixture()
def plugin(tmp_path):
    """构造带真实 store 的最小插件实例(绕过 Star.__init__)。"""
    p = object.__new__(m.LifeSimPlugin)
    p.data_dir = str(tmp_path)
    p.sim_store = m.SimStore(p.data_dir)
    p.rpg_store = m.RpgStore(p.data_dir)
    p.narrative_store = m.NarrativeStore(p.data_dir)
    p.branch_store = m.BranchStore(p.data_dir)
    p.avatar_store = NS(clear_scope=lambda scope: None)
    p._sim_locks = {}
    p._get_sim_lock = lambda key: p._sim_locks.setdefault(key, asyncio.Lock())

    # 种子:一个模拟会话
    session = {
        "mode": "A",
        "owner_name": "小明",
        "created_at": 1700000000,
        "lore_turn": 2,
        "current_branch": "",
        "world_setting": "魔法世界,有龙。",
        "world_lore": [{"seq": 1, "section": "魔法体系", "content": "魔力来自月亮"}],
        "character_lore": {"主角": [{"seq": 1, "section": "appearance", "content": "银发少年"}]},
        "messages": [
            {"role": "user", "turn": 1, "content": "开局"},
            {"role": "assistant", "content": "你出生在……"},
            {"role": "user", "turn": 2, "content": [
                {"type": "text", "text": "看这张图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,XXXX"}},
            ]},
        ],
    }
    write_json_atomic(os.path.join(tmp_path, "sim_sessions", "group_123.json"), session)

    # 种子:剧情历史(主线 + 分支)
    async def seed():
        await p.narrative_store.append(
            "group_123",
            {"user_action": "开局", "summary": "出生", "narrative": "## 0岁\n你出生了"},
        )
        await p.narrative_store.append(
            "group_123",
            {"user_action": "上学", "summary": "入学", "narrative": "## 6岁\n进入魔法学院"},
            branch="if线",
        )
        await p.branch_store.save(
            "group_123",
            "if线",
            {"name": "if线", "saved_at": "2025-01-01T00:00:00+0800", "desc": "转学去魔法学院"},
        )

    asyncio.run(seed())
    return p


def run(coro):
    return asyncio.run(coro)


def as_json(resp):
    """handler 返回 dict 直接用;返回 JSONResponse 时解出 JSON body。"""
    if hasattr(resp, "body"):
        return json.loads(bytes(resp.body))
    return resp


# ── 注册 ────────────────────────────────────────────────────────

def test_register_web_apis():
    registered = {}

    class FakeCtx:
        def register_web_api(self, route, handler, methods, desc):
            registered[(route, tuple(methods))] = handler

    p = object.__new__(m.LifeSimPlugin)
    p.context = FakeCtx()
    p._register_web_apis()

    assert len(registered) == 17
    assert ("/astrbot_plugin_life_sim/api/overview", ("GET",)) in registered
    assert ("/astrbot_plugin_life_sim/api/session/<key>", ("GET",)) in registered
    assert ("/astrbot_plugin_life_sim/api/messages/truncate", ("POST",)) in registered


# ── 总览 / 列表 ─────────────────────────────────────────────────

def test_overview(plugin):
    o = as_json(run(plugin._web_overview()))
    assert o["ok"] is True
    assert o["sessions"]["count"] == 1
    assert o["narrative"]["scopes"] == 1
    assert o["branches"]["files"] == 1
    assert o["rpg"] == {"chars": 0, "sessions": 0}
    assert o["scope_count"] >= 1


def test_scopes(plugin):
    s = as_json(run(plugin._web_scopes()))
    keys = {x["key"] for x in s["scopes"]}
    assert "group_123" in keys
    entry = next(x for x in s["scopes"] if x["key"] == "group_123")
    assert entry["has_session"] is True
    assert entry["branches"] == ["if线"]


def test_sessions_list(plugin):
    s = as_json(run(plugin._web_sessions()))
    assert len(s["sessions"]) == 1
    row = s["sessions"][0]
    assert row["key"] == "group_123"
    assert row["mode_name"]
    assert row["msg_count"] == 3
    assert row["lore_entries"] == 2
    assert row["size"] > 0


# ── 会话详情 ────────────────────────────────────────────────────

def test_session_detail_masks_images(plugin):
    d = as_json(run(plugin._web_session_detail(key="group_123")))
    sess = d["session"]
    assert sess["key"] == "group_123"
    assert sess["world_setting"].startswith("魔法世界")
    assert sess["message_count"] == 3
    img_part = sess["messages"][2]["content"][1]
    assert img_part["image_url"]["url"] == "[图片数据已省略]"
    assert sess["character_lore"]["主角"][0]["content"] == "银发少年"


def test_session_detail_with_images(plugin):
    m._web_request = FakeWebRequest(query={"with_images": "1"})
    d = as_json(run(plugin._web_session_detail(key="group_123")))
    img_part = d["session"]["messages"][2]["content"][1]
    assert img_part["image_url"]["url"].startswith("data:image")


def test_session_detail_not_found(plugin):
    r = as_json(run(plugin._web_session_detail(key="nope")))
    assert r["status"] == "error"


# ── 会话编辑 ────────────────────────────────────────────────────

def test_session_update_basic_and_lore(plugin):
    m._web_request = FakeWebRequest(
        body={
            "key": "group_123",
            "world_setting": "修仙世界。",
            "owner": "小红",
            "world_lore": [
                {"seq": 2, "section": "灵气", "content": "灵气充裕"},
                {"seq": 1, "section": "旧条目", "content": "保留"},
                {"seq": "", "section": "", "content": "自动补 seq"},  # 非法 seq → 自动
            ],
            "character_lore": {
                "主角": [{"seq": 1, "section": "appearance", "content": "黑发"}],
                "路人甲": [],
            },
        }
    )
    r = as_json(run(plugin._web_session_update()))
    assert r["ok"] is True and len(r["changed"]) == 4

    sess = run(plugin.sim_store.load("group_123"))
    assert sess["world_setting"] == "修仙世界。"
    assert sess["owner_name"] == "小红"
    wl = sess["world_lore"]
    assert [e["seq"] for e in wl] == [1, 2, 3]  # 排序 + 自动补号
    assert set(sess["character_lore"]) == {"主角"}  # 空角色被清掉


def test_session_update_bad_body(plugin):
    m._web_request = FakeWebRequest(body={"key": "group_123", "world_lore": "不是数组"})
    r = as_json(run(plugin._web_session_update()))
    assert r["status"] == "error" and "数组" in r["message"]


# ── 消息回滚 ────────────────────────────────────────────────────

def test_truncate_messages(plugin):
    m._web_request = FakeWebRequest(body={"key": "group_123", "keep_messages": 2})
    r = as_json(run(plugin._web_messages_truncate()))
    assert r == {"ok": True, "removed": 1, "kept": 2, "lore_turn": 1}
    sess = run(plugin.sim_store.load("group_123"))
    assert len(sess["messages"]) == 2
    assert sess["lore_turn"] == 1


def test_truncate_out_of_range(plugin):
    m._web_request = FakeWebRequest(body={"key": "group_123", "keep_messages": 99})
    r = as_json(run(plugin._web_messages_truncate()))
    assert r["status"] == "error"


# ── 剧情历史 ────────────────────────────────────────────────────

def test_narrative_list_and_detail(plugin):
    m._web_request = FakeWebRequest(query={"scope": "group_123"})
    lst = as_json(run(plugin._web_narrative_list()))
    assert len(lst["records"]) == 1  # 主线只有 1 条
    rid = lst["records"][0]["id"]

    m._web_request = FakeWebRequest(query={"scope": "group_123", "branch": "if线"})
    lst_b = as_json(run(plugin._web_narrative_list()))
    assert lst_b["records"][0]["id"] != rid

    m._web_request = FakeWebRequest(query={"scope": "group_123", "id": rid})
    det = as_json(run(plugin._web_narrative_detail()))
    assert det["record"]["narrative"] == "## 0岁\n你出生了"


def test_narrative_revise_and_delete(plugin):
    records = run(plugin.narrative_store.list("group_123"))
    rid = records[0]["id"]

    m._web_request = FakeWebRequest(
        body={"scope": "group_123", "id": rid, "narrative": "改写后的剧情"}
    )
    assert as_json(run(plugin._web_narrative_update()))["ok"] is True
    rec = run(plugin.narrative_store.get("group_123", rid))
    assert rec["narrative"] == "改写后的剧情"
    assert rec["revised_count"] == 1

    m._web_request = FakeWebRequest(body={"scope": "group_123", "id": rid})
    assert as_json(run(plugin._web_narrative_delete()))["ok"] is True
    assert run(plugin.narrative_store.get("group_123", rid)) is None


# ── 分支 ────────────────────────────────────────────────────────

def test_branches_list_and_delete(plugin):
    m._web_request = FakeWebRequest(query={"scope": "group_123"})
    lst = as_json(run(plugin._web_branches()))
    assert lst["branches"][0]["name"] == "if线"
    assert lst["branches"][0]["fields"].get("desc") == "转学去魔法学院"

    m._web_request = FakeWebRequest(body={"scope": "group_123", "name": "if线"})
    assert as_json(run(plugin._web_branch_delete()))["ok"] is True
    assert run(plugin.branch_store.list("group_123")) == {}


# ── RPG ─────────────────────────────────────────────────────────

def test_rpg_list_and_deletes(plugin):
    plugin.rpg_store.save_char("10001", {"name": "亚瑟", "level": 3, "hp": 20})
    plugin.rpg_store.save_session("s1", {"game_system": "dnd5e", "members": ["亚瑟"]})

    data = as_json(run(plugin._web_rpg()))
    assert data["chars"][0]["name"] == "亚瑟"
    assert data["sessions"][0]["game_system"] == "dnd5e"

    m._web_request = FakeWebRequest(body={"uid": "10001"})
    assert as_json(run(plugin._web_rpg_char_delete()))["ok"] is True
    m._web_request = FakeWebRequest(body={"sid": "s1"})
    assert as_json(run(plugin._web_rpg_session_delete()))["ok"] is True
    assert plugin.rpg_store.list_chars() == []


def test_rpg_char_delete_missing(plugin):
    m._web_request = FakeWebRequest(body={"uid": "ghost"})
    r = as_json(run(plugin._web_rpg_char_delete()))
    assert r["status"] == "error"


# ── 会话删除(连带清理) ─────────────────────────────────────────

def test_session_delete_purges_all(plugin):
    m._web_request = FakeWebRequest(body={"key": "group_123", "purge_narrative": True})
    r = as_json(run(plugin._web_session_delete()))
    assert r["deleted"]["session"] is True
    assert r["deleted"]["branches"] == 1
    assert r["deleted"]["records"] == 2  # 主线 + if 线(delete_scope 清全部)

    assert run(plugin.sim_store.load("group_123")) is None
    assert os.path.isdir(os.path.join(plugin.data_dir, "narrative_history")) is False or True
    scopes_left = os.listdir(os.path.join(plugin.data_dir, "sim_branches"))
    assert "group_123" not in scopes_left
