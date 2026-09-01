"""剧情历史存档(独立于 sim session 存储)。

按 `scope` 分目录存放,每个 scope **一个文件**:
    <data_dir>/narrative_history/<scope>/history.json

文件结构(单文件,天然便于分支复制/整体覆盖)::

    {
      "_format": 2,
      "versions": {                       # 快照去重表(内容寻址)
        "world_setting":  [...],
        "character_lore": [...],
        "world_lore":     [...]
      },
      "records": [                        # 剧情记录列表(每条带 id)
        {
          "id": "n_<8位hex>", "scope": "...",
          "user_action": ..., "summary": ..., "narrative": ...,
          "source_session_key": ..., "mode": ...,
          "created_at": ..., "revised_at": ..., "revised_count": ...,
          "_ref": {"world_setting": 0, "character_lore": 2, "world_lore": 1}
        }, ...
      ]
    }

设计要点:
- **单文件**:不再一个剧情 id 一个 .json。LLM 工具按 `id` 定位记录、/undo 回滚、
  分支切换整体覆盖,都只读改写这一个文件(原子写),方便可靠。
- **快照去重**:`world_setting / character_lore / world_lore` 三字段在同一 scope
  内连续轮次常完全重复,抽到 `versions` 表只存一份,记录本体存 `_ref` 整数索引;
  读取(list / get)时按 `_ref` 透明展开还原。
- **分支即复制**:`overwrite_all(scope, records)` 用分支里保存的记录整体重建
  history.json(重排版本表),天然实现"分支一份文件"。
- **旧布局自动迁移**:首次访问时若目录里还有老格式(逐条 `n_*.json` / 上一版的
  `_versions.json`),会合并进 history.json 并清理旧文件,幂等。

API:
    append(scope, payload)         - 追加一条,返回 id
    revise(scope, id, narrative)   - 覆盖 narrative + revised_at
    get(scope, id)                 - 读单条(缺 None)
    list(scope)                    - 列本 scope 全部(按 created_at 升序,快照已还原)
    list_all_for_owner(owner_uid)  - 列此用户相关所有 scope 的记录(跨群)
    delete(scope, id)              - 删一条
    delete_scope(scope)            - 清空整个 scope 目录(返回记录数)
    overwrite_all(scope, records)  - 整体覆盖为 records(分支切换)
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import time
from urllib.parse import quote_plus, unquote_plus

from .storage_base import (
    ensure_dir,
    read_json,
    safe_remove,
    sanitize_key,
    write_json_atomic,
)

SUB_DIR = "narrative_history"
HISTORY_FILE = "history.json"
# 老布局残留文件名(迁移用):逐条记录文件前缀 + 独立版本表文件
LEGACY_RECORD_PREFIX = "n_"
VERSIONS_FILE = "_versions.json"
# 分支历史文件前缀:history.json 同目录下 `branch_<编码名>.json`
BRANCH_FILE_PREFIX = "branch_"
# 参与去重的三字段(顺序对齐 `_ref` 键)
SNAP_KEYS = ("world_setting", "character_lore", "world_lore")


def _gen_id() -> str:
    return "n_" + secrets.token_hex(4)


class NarrativeStore:
    """剧情历史存储封装(独立于 sim session),单文件 + 快照去重。"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._root = ensure_dir(os.path.join(data_dir, SUB_DIR))
        # scope → asyncio.Lock,串行化读改写(写操作是整文件原子替换)
        self._locks: dict[str, asyncio.Lock] = {}

    # ── 内部 ───────────────────────────────────────────────
    def _get_lock(self, scope: str) -> asyncio.Lock:
        lk = self._locks.get(scope)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[scope] = lk
        return lk

    def _scope_dir(self, scope: str) -> str:
        safe_scope = sanitize_key(scope)
        if not safe_scope:
            raise ValueError("scope 不能为空")
        return ensure_dir(os.path.join(self._root, safe_scope))

    def _history_path(self, scope: str, branch: str = "") -> str:
        """当前线对应的历史文件:主线(空)用 history.json,分支用 branch_<编码名>.json。"""
        if branch:
            safe = quote_plus(branch, safe="")
            return os.path.join(
                self._scope_dir(scope), f"{BRANCH_FILE_PREFIX}{safe}.json"
            )
        return os.path.join(self._scope_dir(scope), HISTORY_FILE)

    def _branch_name_from_file(self, fname: str) -> str | None:
        """由分支文件名还原分支名(branch_<编码>.json → 名);非分支文件返回 None。"""
        if not fname.startswith(BRANCH_FILE_PREFIX) or not fname.endswith(".json"):
            return None
        stem = fname[len(BRANCH_FILE_PREFIX) : -5]
        try:
            return unquote_plus(stem)
        except ValueError:
            return None

    def list_scopes(self) -> list[str]:
        if not os.path.exists(self._root):
            return []
        return [
            d
            for d in os.listdir(self._root)
            if os.path.isdir(os.path.join(self._root, d))
        ]

    # ── 版本表 / 展开 ──────────────────────────────────────
    @staticmethod
    def _content_idx(values: list, value) -> int:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True)
        for i, v in enumerate(values):
            if json.dumps(v, ensure_ascii=False, sort_keys=True) == key:
                return i
        values.append(value)
        return len(values) - 1

    @staticmethod
    def _build_ref(payload: dict, table: dict) -> dict:
        """把 payload 的三快照字段去重进版本表,返回 `_ref` 索引 dict。"""
        ref: dict[str, int | None] = {}
        for k in SNAP_KEYS:
            if k in payload and payload[k] is not None:
                ref[k] = NarrativeStore._content_idx(table[k], payload[k])
            else:
                ref[k] = None
        return ref

    @staticmethod
    def _expand_record(record: dict, table: dict) -> dict:
        """把记录里的 `_ref` 展开成快照字段;旧记录(无 _ref)原样返回。"""
        if not isinstance(record, dict) or "_ref" not in record:
            return record
        out = dict(record)
        ref = record.get("_ref") or {}
        for k in SNAP_KEYS:
            idx = ref.get(k)
            if isinstance(idx, int) and 0 <= idx < len(table[k]):
                out[k] = table[k][idx]
        return out

    @staticmethod
    def _strip_snap(record: dict) -> dict:
        return {k: v for k, v in record.items() if k not in SNAP_KEYS}

    # ── 读写整文件(含旧布局迁移,仅主线) ───────────────────
    def _load_history(self, scope: str, branch: str = "") -> dict:
        """读指定线(主线/分支)的历史文件;主线发现旧布局时自动迁移合并(幂等)。

        分支文件不存在 → 返回空。返回 {"records": [...], "versions": {...}}。
        """
        scope_dir = self._scope_dir(scope)
        hist_path = self._history_path(scope, branch)
        data = read_json(hist_path)
        if not (isinstance(data, dict) and "records" in data):
            data = {"records": [], "versions": {k: [] for k in SNAP_KEYS}}
        records = list(data.get("records") or [])
        versions = {k: (data.get("versions") or {}).get(k, []) for k in SNAP_KEYS}

        # 旧布局迁移只对主线(history.json)执行:逐条 n_*.json + 独立 _versions.json
        if branch:
            return {"records": records, "versions": versions}
        legacy = sorted(
            f
            for f in os.listdir(scope_dir)
            if f.startswith(LEGACY_RECORD_PREFIX) and f.endswith(".json")
        )
        if not legacy:
            return {"records": records, "versions": versions}

        lv = read_json(os.path.join(scope_dir, VERSIONS_FILE)) or {}
        lv = {k: lv.get(k, []) for k in SNAP_KEYS}
        existing_ids = {r.get("id") for r in records}
        for f in legacy:
            r = read_json(os.path.join(scope_dir, f))
            if not r or r.get("id") in existing_ids:
                continue
            records.append(self._expand_record(r, lv))
            existing_ids.add(r.get("id"))

        # 重建去重:展开的记录重新收敛进版本表
        new_versions = {k: [] for k in SNAP_KEYS}
        new_records: list[dict] = []
        for r in records:
            ref = self._build_ref(r, new_versions)
            nr = self._strip_snap(dict(r))
            nr["_ref"] = ref
            new_records.append(nr)
        data = {"records": new_records, "versions": new_versions}
        write_json_atomic(hist_path, data)
        # 清理旧文件
        for f in legacy:
            safe_remove(os.path.join(scope_dir, f))
        safe_remove(os.path.join(scope_dir, VERSIONS_FILE))
        return data

    def _save_history(self, scope: str, data: dict, branch: str = "") -> None:
        write_json_atomic(self._history_path(scope, branch), data)

    # ── 写入 ───────────────────────────────────────────────

    async def append(self, scope: str, payload: dict, branch: str = "") -> str:
        """向指定线(主线/分支)追加一条新记录,返回 record_id。快照自动去重。"""
        async with self._get_lock(scope + "|" + branch):
            now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            record_id = _gen_id()

            def _write():
                data = self._load_history(scope, branch)
                table = data["versions"]
                ref = self._build_ref(payload, table)
                record = {
                    "id": record_id,
                    "scope": scope,
                    "branch": branch,
                    "created_at": now,
                    "revised_at": now,
                    "_ref": ref,
                    **self._strip_snap(payload),
                }
                data["records"].append(record)
                self._save_history(scope, data, branch)

            await asyncio.to_thread(_write)
            return record_id

    async def revise(
        self, scope: str, record_id: str, narrative: str, branch: str = ""
    ) -> bool:
        """只覆盖 narrative + revised_at + revised_count;快照(_ref)保留。"""
        async with self._get_lock(scope + "|" + branch):
            def _run() -> bool:
                data = self._load_history(scope, branch)
                for r in data["records"]:
                    if r.get("id") == record_id:
                        r["narrative"] = narrative
                        r["revised_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                        r["revised_count"] = int(r.get("revised_count", 0)) + 1
                        self._save_history(scope, data, branch)
                        return True
                return False

            return await asyncio.to_thread(_run)

    async def restore(
        self, scope: str, state: dict, branch: str = ""
    ) -> bool:
        """就地恢复 narrative / revised_count / revised_at(/undo 回滚)。快照不动。"""
        async with self._get_lock(scope + "|" + branch):
            def _run() -> bool:
                record_id = state.get("id")
                if not record_id:
                    return False
                data = self._load_history(scope, branch)
                for r in data["records"]:
                    if r.get("id") == record_id:
                        r["narrative"] = state.get(
                            "narrative", r.get("narrative", "")
                        )
                        r["revised_count"] = int(state.get("revised_count", 0))
                        r["revised_at"] = state.get(
                            "revised_at", r.get("revised_at", "")
                        )
                        self._save_history(scope, data, branch)
                        return True
                return False

            return await asyncio.to_thread(_run)

    # ─── 读取 ───────────────────────────────────────────────

    async def get(self, scope: str, record_id: str, branch: str = "") -> dict | None:
        def _run():
            data = self._load_history(scope, branch)
            for r in data["records"]:
                if r.get("id") == record_id:
                    return self._expand_record(r, data["versions"])
            return None

        return await asyncio.to_thread(_run)

    async def list(self, scope: str, branch: str = "") -> list[dict]:
        """列指定线(主线/分支)全部记录,按 created_at 升序,快照字段还原。"""
        def _run():
            data = self._load_history(scope, branch)
            out = [self._expand_record(r, data["versions"]) for r in data["records"]]
            out.sort(key=lambda r: (r.get("created_at", ""), r.get("id", "")))
            return out

        return await asyncio.to_thread(_run)

    async def list_all_for_owner(
        self, sender_uid: str, current_scope: str = ""
    ) -> list[dict]:
        """列 sender 可见的所有 scope 记录(跨群,含各分支线)。可见性规则见类 docstring。"""
        out: list[dict] = []
        sender_uid = (sender_uid or "").strip()
        for scope in self.list_scopes():
            if scope.startswith("user_"):
                if scope != current_scope and scope != f"user_{sender_uid}":
                    continue
            elif scope.startswith("group_"):
                pass
            else:
                if scope != current_scope:
                    continue
            # 合并主线 + 全部分支线记录
            for line_records in await self._list_all_lines(scope):
                out.extend(line_records)
        out.sort(key=lambda r: r.get("created_at", ""))
        return out

    async def _list_all_lines(self, scope: str) -> list[list[dict]]:
        """列 scope 下每条线(主线 + 各分支)的展开记录,返回 [线记录列表, ...]。"""
        def _run():
            lines: list[list[dict]] = []
            scope_dir = self._scope_dir(scope)
            # 主线
            main = self._load_history(scope, "")
            lines.append([self._expand_record(r, main["versions"]) for r in main["records"]])
            # 分支文件
            if os.path.isdir(scope_dir):
                for f in sorted(os.listdir(scope_dir)):
                    if not f.startswith(BRANCH_FILE_PREFIX) or not f.endswith(".json"):
                        continue
                    name = self._branch_name_from_file(f)
                    if not name:
                        continue
                    data = self._load_history(scope, name)
                    lines.append([self._expand_record(r, data["versions"]) for r in data["records"]])
            return lines

        return await asyncio.to_thread(_run)

    # ─── 删除 ───────────────────────────────────────────────

    async def delete(self, scope: str, record_id: str, branch: str = "") -> bool:
        async with self._get_lock(scope + "|" + branch):
            def _run() -> bool:
                data = self._load_history(scope, branch)
                before = len(data["records"])
                data["records"] = [
                    r for r in data["records"] if r.get("id") != record_id
                ]
                if len(data["records"]) == before:
                    return False
                self._save_history(scope, data, branch)
                return True

            return await asyncio.to_thread(_run)

    async def delete_scope(self, scope: str) -> int:
        """删整个 scope 目录(含主线 history.json / 全部分支历史 / 旧布局残留),返回总记录数。"""
        scope_dir = self._scope_dir(scope)
        if not os.path.exists(scope_dir):
            return 0

        def _purge() -> int:
            count = 0
            # 主线
            data = read_json(os.path.join(scope_dir, HISTORY_FILE))
            if isinstance(data, dict) and "records" in data:
                count += len(data["records"])
            else:
                count += sum(
                    1
                    for f in os.listdir(scope_dir)
                    if f.startswith(LEGACY_RECORD_PREFIX) and f.endswith(".json")
                )
            # 分支历史文件
            for f in sorted(os.listdir(scope_dir)):
                if not f.startswith(BRANCH_FILE_PREFIX) or not f.endswith(".json"):
                    continue
                d = read_json(os.path.join(scope_dir, f))
                count += len((d or {}).get("records") or [])
            shutil.rmtree(scope_dir, ignore_errors=True)
            return count

        return await asyncio.to_thread(_purge)

    async def overwrite_all(
        self, scope: str, records: list[dict], branch: str = ""
    ) -> dict:
        """把指定线(主线/分支)的剧情历史整体覆盖为 records(旧分支回退用),快照重新去重。"""
        async with self._get_lock(scope + "|" + branch):
            def _overwrite() -> tuple[int, int]:
                old = self._load_history(scope, branch)
                table = {k: [] for k in SNAP_KEYS}
                new_records: list[dict] = []
                for r in records:
                    if not r.get("id"):
                        continue
                    ref = self._build_ref(r, table)
                    nr = dict(r)
                    nr["scope"] = scope
                    nr["branch"] = branch
                    nr["_ref"] = ref
                    # 展开的输入里通常带着快照字段,收敛进版本表后移除,保持单文件精简
                    nr = self._strip_snap(nr)
                    new_records.append(nr)
                data = {"records": new_records, "versions": table}
                self._save_history(scope, data, branch)
                return len(new_records), len(old["records"]) - len(new_records)

            written, deleted = await asyncio.to_thread(_overwrite)
            return {"written": written, "deleted": deleted}

    # ─── 分支文件(与 history.json 同目录,各自独立) ─────────

    async def branch_exists(self, scope: str, branch_name: str) -> bool:
        """指定分支的历史文件是否存在。"""
        path = self._history_path(scope, branch_name)
        return await asyncio.to_thread(os.path.exists, path)

    async def save_branch_history(
        self,
        scope: str,
        branch_name: str,
        source_branch: str = "",
    ) -> bool:
        """把源线(默认主线)的历史归档为分支文件(复制文件,versions 随行)。"""
        if not branch_name:
            return False
        async with self._get_lock(scope + "|" + branch_name):
            def _run() -> bool:
                data = self._load_history(scope, source_branch)
                snapshot = {
                    "_format": 2,
                    "branch": branch_name,
                    "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "versions": data["versions"],
                    "records": data["records"],
                }
                write_json_atomic(self._history_path(scope, branch_name), snapshot)
                return True

            return await asyncio.to_thread(_run)

    async def load_branch_history(self, scope: str, branch_name: str) -> dict | None:
        """读某分支的历史文件(展开后的 records + versions),缺返回 None。"""
        def _run():
            data = read_json(self._history_path(scope, branch_name))
            if not isinstance(data, dict) or "records" not in data:
                return None
            versions = {k: (data.get("versions") or {}).get(k, []) for k in SNAP_KEYS}
            return {
                "records": [
                    self._expand_record(r, versions) for r in (data.get("records") or [])
                ],
                "versions": versions,
                "branch": branch_name,
            }

        return await asyncio.to_thread(_run)

    async def delete_branch_history(self, scope: str, branch_name: str) -> bool:
        """删除某分支的历史文件。"""
        return await asyncio.to_thread(safe_remove, self._history_path(scope, branch_name))

    async def list_branch_histories(self, scope: str) -> dict[str, dict]:
        """列出 scope 下全部分支历史(分支名 → {saved_at, record_count})。"""
        scope_dir = self._scope_dir(scope)

        def _run():
            if not os.path.isdir(scope_dir):
                return {}
            out: dict[str, dict] = {}
            for f in os.listdir(scope_dir):
                if not f.startswith(BRANCH_FILE_PREFIX) or not f.endswith(".json"):
                    continue
                name = self._branch_name_from_file(f)
                if not name:
                    continue
                d = read_json(os.path.join(scope_dir, f))
                out[name] = {
                    "saved_at": (d or {}).get("saved_at", ""),
                    "record_count": len((d or {}).get("records") or []),
                }
            return out

        return await asyncio.to_thread(_run)