"""/选择性加载 lore 测试:活跃角色检测、注入裁剪、按需读取工具。

运行方式(在插件根目录):
    .venv/bin/python tests/test_lore_selective.py
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

from lsim_pkg.main import LifeSimPlugin
from lsim_pkg.storage_sim import SimStore


class _Event:
    def __init__(self, gid, sid):
        self.group_id = gid
        self.sender_id = sid
        self.message_obj = type("o", (), {"group_id": gid, "timestamp": time.time()})()

    def get_sender_id(self):
        return self.sender_id


class _FakePlugin:
    """只实现 lore 选择性加载路径所需方法。"""

    def __init__(self, data_dir, cfg=None):
        self.data_dir = data_dir
        self.sim_store = SimStore(data_dir)
        self.cfg = cfg or {}
        self._pending_lore = {}

    def _cfg(self, key, default=None):
        return self.cfg.get(key, default)

    def _sim_session_key(self, event):
        return f"group_{event.group_id}" if event.group_id else f"user_{event.sender_id}"

    async def _load_sim(self, event):
        return await self.sim_store.load(self._sim_session_key(event))

    # 借 LifeSimPlugin 的实现
    _normalize_character_lore = staticmethod(LifeSimPlugin._normalize_character_lore)
    _render_lore_timeline = staticmethod(LifeSimPlugin._render_lore_timeline)

    def _detect_active_characters(self, session, rounds, extra_text=""):
        return LifeSimPlugin._detect_active_characters(self, session, rounds, extra_text)

    def _build_lore_addendum(self, session, current_input=""):
        return LifeSimPlugin._build_lore_addendum(self, session, current_input)

    async def life_sim_get_character_lore(self, event, character="主角"):
        return await LifeSimPlugin.life_sim_get_character_lore(self, event, character)

    async def life_sim_get_world_lore(self, event, section=""):
        return await LifeSimPlugin.life_sim_get_world_lore(self, event, section)


def _session_with_lore():
    """主角(2条) + 两个 NPC 各 3 条;最近几轮只提到主角和 导师·长者。"""
    return {
        "world_setting": "奇幻世界",
        "mode": "A",
        "lore_turn": 10,
        "messages": [
            {"role": "user", "content": "开局"},
            {"role": "assistant", "content": "主角在山脚下醒来"},
            {"role": "user", "content": "去城镇"},
            {"role": "assistant", "content": "主角来到城镇,与导师·长者交谈"},
            {"role": "user", "content": "继续"},
            {"role": "assistant", "content": "主角告别导师·长者,前往森林"},
        ],
        "character_lore": {
            "主角": [
                {"seq": 1, "section": "appearance", "content": "赤红短发、琥珀色眼瞳", "updated_at": "t1"},
                {"seq": 2, "section": "personality", "content": "勇敢但冲动", "updated_at": "t2"},
            ],
            "导师·长者": [
                {"seq": 1, "section": "appearance", "content": "银白长须、灰袍", "updated_at": "t1"},
                {"seq": 2, "section": "personality", "content": "沉稳睿智", "updated_at": "t2"},
                {"seq": 3, "section": "skills", "content": "高阶法术", "updated_at": "t3"},
            ],
            "森林之王": [
                {"seq": 1, "section": "appearance", "content": "金色鬃毛巨兽", "updated_at": "t1"},
                {"seq": 2, "section": "personality", "content": "高傲", "updated_at": "t2"},
                {"seq": 3, "section": "skills", "content": "森林领域", "updated_at": "t3"},
            ],
        },
        "world_lore": [
            {"seq": 1, "section": "魔法体系", "content": "元素魔法为主", "updated_at": "t1"},
            {"seq": 2, "section": "魔法体系", "content": "新增禁忌魔法分类", "updated_at": "t5"},
            {"seq": 1, "section": "地理", "content": "大陆分五国", "updated_at": "t1"},
        ],
    }


async def test_detect_active():
    p = _FakePlugin("/tmp")
    s = _session_with_lore()
    active = p._detect_active_characters(s, 6)
    assert "主角" in active, active
    assert "导师·长者" in active, active
    assert "森林之王" not in active, active
    # 窗口=1 轮:只看最后一条 assistant(主角告别导师),导师·长者出现
    active1 = p._detect_active_characters(s, 1)
    assert "导师·长者" in active1 and "森林之王" not in active1, active1
    print("detect_active OK")


async def test_selective_inject():
    p = _FakePlugin("/tmp", {"lore_selective_load": True, "lore_active_rounds": 6})
    s = _session_with_lore()
    text = p._build_lore_addendum(s)

    # 活跃角色完整注入
    assert "赤红短发、琥珀色眼瞳" in text          # 主角 appearance
    assert "银白长须、灰袍" in text                # 导师·长者 appearance
    assert "高阶法术" in text                      # 导师·长者 skills

    # 非活跃角色只留摘要行,不含其设定内容
    assert "森林之王" in text
    assert "金色鬃毛巨兽" not in text, "非活跃角色不应注入完整设定"
    assert "life_sim_get_character_lore" in text
    assert "未出场" in text

    # 世界观:始终完整注入(不做按需裁剪,魔法体系 2 条全部注入)
    assert "新增禁忌魔法分类" in text
    assert "元素魔法为主" in text, "世界观完整注入,历史条目也在"
    assert "大陆分五国" in text
    print("selective inject OK")
    print("--- 注入长度:", len(text), "字符")


async def test_full_inject():
    p = _FakePlugin("/tmp", {"lore_selective_load": False})
    s = _session_with_lore()
    text = p._build_lore_addendum(s)
    # 全部完整注入(旧行为)
    assert "赤红短发、琥珀色眼瞳" in text
    assert "金色鬃毛巨兽" in text
    assert "元素魔法为主" in text
    assert "未出场" not in text
    print("full inject OK")


async def test_read_tools():
    tmp = tempfile.mkdtemp(prefix="lore_test_")
    try:
        p = _FakePlugin(tmp)
        ev = _Event("g1", "u1")
        session = _session_with_lore()
        await p.sim_store.save(p._sim_session_key(ev), session)

        # 读取完整角色设定
        out = await p.life_sim_get_character_lore(ev, "森林之王")
        assert "金色鬃毛巨兽" in out and "森林领域" in out, out
        # 读取不存在的角色 → 返回候选列表
        out2 = await p.life_sim_get_character_lore(ev, "不存在的人")
        assert "已收录角色" in out2 and "森林之王" in out2, out2
        # 空 character → 返回列表
        out3 = await p.life_sim_get_character_lore(ev, "")
        assert "已收录角色" in out3, out3
        # 按 section 读世界观
        out4 = await p.life_sim_get_world_lore(ev, "魔法体系")
        assert "元素魔法为主" in out4 and "新增禁忌魔法分类" in out4, out4
        out5 = await p.life_sim_get_world_lore(ev, "不存在的分类")
        assert "已有 section" in out5, out5
        # 读全部世界观
        out6 = await p.life_sim_get_world_lore(ev, "")
        assert "大陆分五国" in out6, out6
        print("read tools OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_staging_merge():
    """同轮内先 save 再 get,应能读到 staging 里的新条目。"""
    tmp = tempfile.mkdtemp(prefix="lore_test2_")
    try:
        p = _FakePlugin(tmp)
        ev = _Event("g1", "u1")
        session = _session_with_lore()
        await p.sim_store.save(p._sim_session_key(ev), session)
        # 模拟 _generate 初始化 staging
        p._pending_lore[p._sim_session_key(ev)] = {}
        # 同轮 save 新条目(直接写 staging,模拟 _save_lore)
        p._pending_lore[p._sim_session_key(ev)]["character_lore"] = {
            "森林之王": [{"seq": 4, "section": "skills", "content": "解锁新领域", "updated_at": "t9"}]
        }
        out = await p.life_sim_get_character_lore(ev, "森林之王")
        assert "解锁新领域" in out, out
        print("staging merge OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_alias_detect():
    """别名/昵称匹配:括号内昵称、同人多 key、单字边界。"""
    from lsim_pkg.main import _char_aliases

    # 雪音显式要求:小+首字 → 小雪/阿雪/雪酱(首字黑名单不含"雪")
    assert "小雪" in _char_aliases("雪音")
    assert "小音" in _char_aliases("雪音")
    # 梦娜1号:去编号后缀("1号")→ 梦娜;昵称基于干净主干
    assert "梦娜" in _char_aliases("梦娜1号")
    assert "小娜" in _char_aliases("梦娜1号")
    # 高频"小明"仍被挡,但"小香"(末字变体)不被首字黑名单连坐
    assert "小明" not in _char_aliases("明日香")
    assert "小香" in _char_aliases("明日香")
    # "明日香"：首字"明"在黑名单 → 无"小明"；末字"香"有"小香/阿香/香酱"
    assert set(_char_aliases("明日香")) == {"明日香", "日香", "阿明", "明酱", "小香", "阿香", "香酱"}
    assert set(_char_aliases("商人(艾)")) == {"商人(艾)", "商人", "小艾", "阿艾", "艾酱"}
    # 中文称呼截取:全名末尾 2 字(去分隔符)
    assert "银时" in _char_aliases("坂田银时")
    assert "柯南" in _char_aliases("江户川柯南")
    assert "悟空" in _char_aliases("孙悟空")
    assert "长者" in _char_aliases("导师·长者")
    # 昵称变体:汐见花音 → 花音 / 小音 / 阿音 / 音酱
    aliases = _char_aliases("汐见花音")
    for expect in ("花音", "小音", "阿音", "音酱"):
        assert expect in aliases, (expect, aliases)
    # 常见末字的 "小X" 昵称被跳过,避免 "小时" 等高频词误伤
    assert "小时" not in _char_aliases("坂田银时")

    class _F:
        def __init__(self):
            self.cfg = {}

        def _cfg(self, k, d=None):
            return self.cfg.get(k, d)

        _normalize_character_lore = staticmethod(
            LifeSimPlugin._normalize_character_lore
        )

        def _detect_active_characters(self, s, r):
            return LifeSimPlugin._detect_active_characters(self, s, r)

    f = _F()

    def session_of(content, lore):
        return {
            "messages": [{"role": "assistant", "content": content}],
            "character_lore": lore,
        }

    # 文本提昵称「小花」→ 全名 key 活跃
    a = f._detect_active_characters(
        session_of("主角与小花并肩而行",
                   {"花原（小花）": [{"seq": 1, "section": "appearance", "content": "红发", "updated_at": "t"}]}),
        6,
    )
    assert "花原（小花）" in a, a
    # 同人多 key 一起活跃
    a = f._detect_active_characters(
        session_of("主角遇见了小花",
                   {"花原（小花）": [{"seq": 1, "section": "appearance", "content": "红发", "updated_at": "t"}],
                    "小花": [{"seq": 1, "section": "personality", "content": "温柔", "updated_at": "t"}]}),
        6,
    )
    assert "花原（小花）" in a and "小花" in a, a
    # 单字昵称边界:文本提「香」不匹配「明日香」(启发式,避免误伤)
    a = f._detect_active_characters(
        session_of("香推门走进来", {"明日香": [{"seq": 1, "section": "appearance", "content": "红发", "updated_at": "t"}]}),
        6,
    )
    assert "明日香" not in a, a
    # 中文称呼截取:角色 key "坂田银时",上下文只提 "银时"
    a = f._detect_active_characters(
        session_of("银时懒洋洋地躺在沙发上",
                   {"坂田银时": [{"seq": 1, "section": "appearance", "content": "银色天然卷", "updated_at": "t"}]}),
        6,
    )
    assert "坂田银时" in a, a
    # 「明日」常见词不误触「明日香」(tail 是 "日香" 而非 "明日")
    a = f._detect_active_characters(
        session_of("明天一早就要出发", {"明日香": [{"seq": 1, "section": "appearance", "content": "红发", "updated_at": "t"}]}),
        6,
    )
    assert "明日香" not in a, a
    # 昵称变体:汐见花音,上下文只提 "小音" / "花音" / "音酱" 都判活跃
    lore = {"汐见花音": [{"seq": 1, "section": "appearance", "content": "粉色短发", "updated_at": "t"}]}
    for mention in ("小音", "花音", "音酱", "汐见花音"):
        a = f._detect_active_characters(
            session_of(f"主角对{mention}笑了笑", lore), 6
        )
        assert "汐见花音" in a, (mention, a)
    # 当前用户输入也参与活跃检测:历史没提到,但当前输入点名 → 立即活跃
    class _F2:
        def __init__(self):
            self.cfg = {}

        def _cfg(self, k, d=None):
            return self.cfg.get(k, d)

        _normalize_character_lore = staticmethod(
            LifeSimPlugin._normalize_character_lore
        )

        def _detect_active_characters(self, s, r, e=""):
            return LifeSimPlugin._detect_active_characters(self, s, r, e)

    f2 = _F2()
    hist = session_of("主角独自走在山路上", lore)
    a = f2._detect_active_characters(hist, 6)  # 无当前输入
    assert "汐见花音" not in a, a
    a = f2._detect_active_characters(hist, 6, "我决定去找汐见花音帮忙")  # 当前输入点名
    assert "汐见花音" in a, a
    # 常见词不误触:文本含 "小时" 不激活 "坂田银时"
    a = f._detect_active_characters(
        session_of("等了两个小时才出发",
                   {"坂田银时": [{"seq": 1, "section": "appearance", "content": "银色卷发", "updated_at": "t"}]}),
        6,
    )
    assert "坂田银时" not in a, a
    # 纯单字名不误判
    a = f._detect_active_characters(
        session_of("花开花落,香气袭人",
                   {"花": [{"seq": 1, "section": "p", "content": "x", "updated_at": "t"}]}),
        6,
    )
    assert not a, a
    print("alias detect OK")


async def test_multi_key_match():
    """同一个人被拆成多个 key 时,读取工具返回匹配到的全部 lore。"""
    from lsim_pkg.main import _match_lore_characters

    lore = {
        "汐见花音": [{"seq": 1, "section": "appearance", "content": "粉色短发", "updated_at": "t"}],
        "花音": [{"seq": 1, "section": "personality", "content": "温柔", "updated_at": "t"}],
        "坂田银时": [{"seq": 1, "section": "appearance", "content": "银色卷发", "updated_at": "t"}],
        "主角": [{"seq": 1, "section": "p", "content": "x", "updated_at": "t"}],
    }
    assert _match_lore_characters(lore, "花音") == ["汐见花音", "花音"]
    assert _match_lore_characters(lore, "汐见花音") == ["汐见花音", "花音"]  # 名称互相包含
    assert _match_lore_characters(lore, "银时") == ["坂田银时"]
    assert _match_lore_characters(lore, "小音") == ["汐见花音", "花音"]
    assert _match_lore_characters(lore, "主角") == ["主角"]
    assert _match_lore_characters(lore, "花") == []  # 单字不做名称包含
    assert _match_lore_characters(lore, "") == []

    # 活跃检测对称性:文本提全名/短名/昵称,两个 key 都激活
    # (活跃检测是子串匹配:文本含全名"汐见花音"时天然包含"花音",故天然对称)
    class _Det:
        def __init__(self):
            self.cfg = {}

        def _cfg(self, k, d=None):
            return self.cfg.get(k, d)

        _normalize_character_lore = staticmethod(
            LifeSimPlugin._normalize_character_lore
        )

        def _detect_active_characters(self, s, r):
            return LifeSimPlugin._detect_active_characters(self, s, r)

    det = _Det()
    for mention in ("汐见花音", "花音", "小音"):
        a = det._detect_active_characters(
            {"messages": [{"role": "assistant", "content": f"{mention}走进来"}], "character_lore": lore},
            6,
        )
        assert a == {"汐见花音", "花音"}, (mention, a)

    # 读取工具端到端:传 "花音" 返回两个 key 的完整 lore
    import tempfile as _tf, shutil as _sh
    from lsim_pkg.storage_sim import SimStore as _SS

    tmp = _tf.mkdtemp(prefix="lore_multi_")
    try:
        class _F:
            def __init__(self):
                self.data_dir = tmp
                self.sim_store = _SS(tmp)
                self._pending_lore = {}

            def _sim_session_key(self, e):
                return f"group_{e.group_id}"

            async def _load_sim(self, e):
                return await self.sim_store.load(self._sim_session_key(e))

            _normalize_character_lore = staticmethod(
                LifeSimPlugin._normalize_character_lore
            )
            _render_lore_timeline = staticmethod(LifeSimPlugin._render_lore_timeline)

            async def life_sim_get_character_lore(self, e, character="主角"):
                return await LifeSimPlugin.life_sim_get_character_lore(self, e, character)

        f = _F()
        ev = type("E", (), {"group_id": "g1"})()
        await f.sim_store.save("group_g1", {"world_setting": "w", "mode": "A", "messages": [], "character_lore": lore})
        out = await f.life_sim_get_character_lore(ev, "花音")
        assert "共匹配到 2 个角色 key" in out, out
        assert "汐见花音" in out and "花音" in out and "粉色短发" in out and "温柔" in out
        # 数组批量查询:一次查多个角色,无需多次调用
        out_arr = await f.life_sim_get_character_lore(ev, ["花音", "银时"])
        assert "共匹配到 3 个角色 key" in out_arr, out_arr[:120]
        assert "汐见花音" in out_arr and "坂田银时" in out_arr
        assert "粉色短发" in out_arr and "银色卷发" in out_arr
        # 数组含无效项:只返回有效的
        out_part = await f.life_sim_get_character_lore(ev, ["不存在", "银时"])
        assert "坂田银时" in out_part
        # 数组全无效 → 错误提示
        out_none = await f.life_sim_get_character_lore(ev, ["不存在", "x"])
        assert "没有匹配到" in out_none
        # 空数组 → 列全部
        out_all = await f.life_sim_get_character_lore(ev, [])
        assert "已收录角色" in out_all and "汐见花音" in out_all
        # 精确单 key 不出现提示
        out2 = await f.life_sim_get_character_lore(ev, "主角")
        assert "共匹配" not in out2
        # 无匹配
        out3 = await f.life_sim_get_character_lore(ev, "不存在")
        assert "没有匹配到" in out3 and "已收录角色" in out3
    finally:
        _sh.rmtree(tmp, ignore_errors=True)
    print("multi-key match OK")


async def main():
    await test_detect_active()
    await test_selective_inject()
    await test_full_inject()
    await test_read_tools()
    await test_staging_merge()
    await test_alias_detect()
    await test_multi_key_match()
    print("ALL LORE SELECTIVE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
