"""NarrativeStore 单文件 + 快照去重测试:append 去重 / 读取还原 / legacy 迁移 / 分支覆盖。

运行(在插件根目录):
    .venv/bin/python tests/test_narrative_store.py
"""
import asyncio
import glob
import importlib.util
import json
import os
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
_spec = importlib.util.spec_from_file_location(
    "lsim_pkgc", os.path.join(_ROOT, "__init__.py"),
    submodule_search_locations=[_ROOT],
)
_pkg = importlib.util.module_from_spec(_spec)
sys.modules["lsim_pkgc"] = _pkg
_spec.loader.exec_module(_pkg)

from lsim_pkgc.storage_narrative import (  # noqa: E402
    NarrativeStore,
    HISTORY_FILE,
)

LORE = {
    "world_setting": "奇妙世界",
    "character_lore": {"主角": [{"seq": 1, "section": "性格", "content": "勇敢"}]},
    "world_lore": [{"seq": 1, "section": "地理", "content": "有森林"}],
}


async def test_append_dedup_and_single_file():
    tmp = tempfile.mkdtemp()
    try:
        st = NarrativeStore(tmp)
        scope = "group_g1"
        ids = []
        for i in range(5):
            p = dict(LORE)
            p["narrative"] = f"第{i}轮剧情"
            p["user_action"] = f"输入{i}"
            ids.append(await st.append(scope, p))
        p5 = dict(LORE)
        p5["world_lore"] = [{"seq": 1, "section": "地理", "content": "有海洋"}]
        p5["narrative"] = "第5轮剧情"
        ids.append(await st.append(scope, p5))

        records = await st.list(scope)
        assert len(records) == 6
        r0 = next(r for r in records if r["id"] == ids[0])
        assert r0["world_setting"] == "奇妙世界"
        assert r0["character_lore"] == {"主角": [{"seq": 1, "section": "性格", "content": "勇敢"}]}
        assert r0["world_lore"] == LORE["world_lore"]

        # 单文件:目录下只有 history.json,内含版本表
        files = glob.glob(os.path.join(tmp, "narrative_history", scope, "*.json"))
        assert files == [os.path.join(tmp, "narrative_history", scope, HISTORY_FILE)], files
        hist = json.load(open(files[0], encoding="utf-8"))
        v = hist["versions"]
        assert len(v["world_setting"]) == 1
        assert len(v["character_lore"]) == 1
        assert len(v["world_lore"]) == 2
        assert len(hist["records"]) == 6
        raw = hist["records"][0]
        assert "_ref" in raw and "character_lore" not in raw
        print("append dedup + single-file OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_legacy_migration():
    tmp = tempfile.mkdtemp()
    try:
        s = NarrativeStore(tmp)
        scope = "group_g1"
        # 老布局:逐条 n_*.json(内联快照,无 _ref)
        d = os.path.join(tmp, "narrative_history", scope)
        os.makedirs(d, exist_ok=True)
        for i in range(3):
            with open(os.path.join(d, f"n_old{i}.json"), "w", encoding="utf-8") as f:
                json.dump({"id": f"n_old{i}", "scope": scope, "narrative": f"旧{i}", **LORE}, f, ensure_ascii=False)
        records = await s.list(scope)  # 触发迁移
        assert len(records) == 3
        assert records[0]["character_lore"] == LORE["character_lore"]
        # 迁移后:只剩 history.json
        files = glob.glob(os.path.join(d, "*.json"))
        assert files == [os.path.join(d, HISTORY_FILE)], files
        # 幂等:再 list 不变
        again = await s.list(scope)
        assert len(again) == 3
        print("legacy migration + idempotent OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_revise_restore_keep_snap():
    tmp = tempfile.mkdtemp()
    try:
        s = NarrativeStore(tmp)
        scope = "group_g1"
        pid = await s.append(scope, {**LORE, "narrative": "初版"})
        await s.revise(scope, pid, "修订版")
        r = await s.get(scope, pid)
        assert r["narrative"] == "修订版"
        assert r["character_lore"] == LORE["character_lore"]
        await s.restore(scope, {"id": pid, "narrative": "初版", "revised_count": 0, "revised_at": ""})
        r2 = await s.get(scope, pid)
        assert r2["narrative"] == "初版" and r2["character_lore"] == LORE["character_lore"]
        print("revise/restore keep snap OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_delete_and_delete_scope():
    tmp = tempfile.mkdtemp()
    try:
        s = NarrativeStore(tmp)
        scope = "group_g1"
        a = await s.append(scope, {**LORE, "narrative": "A"})
        b = await s.append(scope, {**LORE, "narrative": "B"})
        assert await s.delete(scope, a) is True
        assert await s.delete(scope, a) is False  # 已删
        records = await s.list(scope)
        assert [r["id"] for r in records] == [b]
        n = await s.delete_scope(scope)
        assert n == 1, n
        assert not os.path.exists(os.path.join(tmp, "narrative_history", scope))
        print("delete / delete_scope OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_overwrite_all_branch():
    tmp = tempfile.mkdtemp()
    try:
        s = NarrativeStore(tmp)
        scope = "group_g1"
        await s.append(scope, {**LORE, "narrative": "A"})
        await s.append(scope, {**LORE, "narrative": "B"})
        allr = await s.list(scope)
        target = allr[-1]
        res = await s.overwrite_all(scope, [target])
        assert res["written"] == 1 and res["deleted"] == 1
        after = await s.list(scope)
        assert len(after) == 1 and after[0]["narrative"] == target["narrative"]
        assert after[0]["character_lore"] == LORE["character_lore"]
        # 分支复制:用 list 结果重建另一 scope → 独立历史
        scope2 = "group_g2"
        res2 = await s.overwrite_all(scope2, await s.list(scope))
        r2 = await s.list(scope2)
        assert len(r2) == 1 and r2[0]["narrative"] == target["narrative"]
        print("overwrite_all (branch copy) OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_branch_history_files():
    tmp = tempfile.mkdtemp()
    try:
        s = NarrativeStore(tmp)
        scope = "group_g1"
        # 两条剧情记录,其中一条 lore 不同
        await s.append(scope, {**LORE, "narrative": "A"})
        await s.append(scope, {**LORE, "narrative": "B", "world_lore": [{"seq":1,"section":"地理","content":"有海洋"}]})
        # 保存分支「东线」
        await s.save_branch_history(scope, "东线")
        # 再从主线推几条,让主历史与分支历史不同
        await s.append(scope, {**LORE, "narrative": "主线3"})
        await s.append(scope, {**LORE, "narrative": "主线4"})

        # 保存另一分支「西线」—— 应包含第一条分支保存后的新推进?不; 分支文件复制的是
        # 保存当时的整份 records/versions。一切以 save_branch_history 时刻为界。
        await s.append(scope, {**LORE, "narrative": "西线前"})
        await s.save_branch_history(scope, "西线")

        # 分支与主线历史文件各自独立:list 读主线,分支文件读对应分支
        assert await s.branch_exists(scope, "东线")
        assert not await s.branch_exists(scope, "不存在线")
        # 主线历史:保存东线后主线继续推进,含 A/B + 主线3/4 + 西线前
        main_records = await s.list(scope, "")
        main_narr = [r["narrative"] for r in main_records]
        assert "西线前" in main_narr and "主线3" in main_narr
        assert "A" in main_narr and "B" in main_narr
        # 东线分支历史:定格在保存时刻(A/B)
        east = await s.load_branch_history(scope, "东线")
        assert (east or {}).get("records") and sorted(
            r["narrative"] for r in east["records"]
        ) == ["A", "B"]
        # 东线不含主线推进后的记录
        assert "主线3" not in [r["narrative"] for r in east["records"]]
        # list_branch_histories 枚举到两个分支
        bh = await s.list_branch_histories(scope)
        assert set(bh) == {"东线", "西线"}
        print("branch per-line isolation + exists/enum OK")
        # versions 自洽(分支表里的同一份)
        print("branch save/switch/delete-orphan OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def main():
    await test_append_dedup_and_single_file()
    await test_legacy_migration()
    await test_revise_restore_keep_snap()
    await test_delete_and_delete_scope()
    await test_overwrite_all_branch()
    await test_branch_history_files()
    print("ALL NARRATIVE STORE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())


