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

    def _history_path(self, scope: str) -> str:
        return os.path.join(self._scope_dir(scope), HISTORY_FILE)

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

    # ── 读写整文件(含旧布局迁移) ───────────────────────────
    def _load_history(self, scope: str) -> dict:
        """读 scope 的 history.json;发现旧布局时自动迁移合并(幂等)。

        返回 {"records": [...], "versions": {...}}。迁移后旧文件被清理。
        """
        scope_dir = self._scope_dir(scope)
        hist_path = os.path.join(scope_dir, HISTORY_FILE)
        data = read_json(hist_path)
        if not (isinstance(data, dict) and "records" in data):
            data = {"records": [], "versions": {k: [] for k in SNAP_KEYS}}
        records = list(data.get("records") or [])
        versions = {k: (data.get("versions") or {}).get(k, []) for k in SNAP_KEYS}

        # 旧布局:逐条 n_*.json(可能带独立 _versions.json)
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

    def _save_history(self, scope: str, data: dict) -> None:
        write_json_atomic(self._history_path(scope), data)

    # ── 写入 ───────────────────────────────────────────────

    async def append(self, scope: str, payload: dict) -> str:
        """追加一条新记录,返回 record_id。快照字段自动去重。"""
        async with self._get_lock(scope):
            now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            record_id = _gen_id()

            def _write():
                data = self._load_history(scope)
                table = data["versions"]
                ref = self._build_ref(payload, table)
                record = {
                    "id": record_id,
                    "scope": scope,
                    "created_at": now,
                    "revised_at": now,
                    "_ref": ref,
                    **self._strip_snap(payload),
                }
                data["records"].append(record)
                self._save_history(scope, data)

            await asyncio.to_thread(_write)
            return record_id

    async def revise(self, scope: str, record_id: str, narrative: str) -> bool:
        """只覆盖 narrative + revised_at + revised_count;快照(_ref)保留。"""
        async with self._get_lock(scope):
            def _run() -> bool:
                data = self._load_history(scope)
                for r in data["records"]:
                    if r.get("id") == record_id:
                        r["narrative"] = narrative
                        r["revised_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                        r["revised_count"] = int(r.get("revised_count", 0)) + 1
                        self._save_history(scope, data)
                        return True
                return False

            return await asyncio.to_thread(_run)

    async def restore(self, scope: str, state: dict) -> bool:
        """就地恢复 narrative / revised_count / revised_at(/undo 回滚)。快照不动。"""
        async with self._get_lock(scope):
            def _run() -> bool:
                record_id = state.get("id")
                if not record_id:
                    return False
                data = self._load_history(scope)
                for r in data["records"]:
                    if r.get("id") == record_id:
                        r["narrative"] = state.get(
                            "narrative", r.get("narrative", "")
                        )
                        r["revised_count"] = int(state.get("revised_count", 0))
                        r["revised_at"] = state.get(
                            "revised_at", r.get("revised_at", "")
                        )
                        self._save_history(scope, data)
                        return True
                return False

            return await asyncio.to_thread(_run)

    # ─── 读取 ───────────────────────────────────────────────

    async def get(self, scope: str, record_id: str) -> dict | None:
        def _run():
            data = self._load_history(scope)
            for r in data["records"]:
                if r.get("id") == record_id:
                    return self._expand_record(r, data["versions"])
            return None

        return await asyncio.to_thread(_run)

    async def list(self, scope: str) -> list[dict]:
        """列本 scope 全部记录,按 created_at 升序,快照字段还原。"""
        def _run():
            data = self._load_history(scope)
            out = [self._expand_record(r, data["versions"]) for r in data["records"]]
            out.sort(key=lambda r: (r.get("created_at", ""), r.get("id", "")))
            return out

        return await asyncio.to_thread(_run)

    async def list_all_for_owner(
        self, sender_uid: str, current_scope: str = ""
    ) -> list[dict]:
        """列 sender 可见的所有 scope 记录。可见性规则见类 docstring。"""
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
            out.extend(await self.list(scope))
        out.sort(key=lambda r: r.get("created_at", ""))
        return out

    # ─── 删除 ───────────────────────────────────────────────

    async def delete(self, scope: str, record_id: str) -> bool:
        async with self._get_lock(scope):
            def _run() -> bool:
                data = self._load_history(scope)
                before = len(data["records"])
                data["records"] = [
                    r for r in data["records"] if r.get("id") != record_id
                ]
                if len(data["records"]) == before:
                    return False
                self._save_history(scope, data)
                return True

            return await asyncio.to_thread(_run)

    async def delete_scope(self, scope: str) -> int:
        """删整个 scope 目录(含 history.json / 旧布局残留),返回记录数。"""
        scope_dir = self._scope_dir(scope)
        if not os.path.exists(scope_dir):
            return 0

        def _purge() -> int:
            count = 0
            data = read_json(os.path.join(scope_dir, HISTORY_FILE))
            if isinstance(data, dict) and "records" in data:
                count = len(data["records"])
            else:
                # 旧布局:数一下逐条文件
                count = sum(
                    1
                    for f in os.listdir(scope_dir)
                    if f.startswith(LEGACY_RECORD_PREFIX) and f.endswith(".json")
                )
            shutil.rmtree(scope_dir, ignore_errors=True)
            return count

        return await asyncio.to_thread(_purge)

    async def overwrite_all(self, scope: str, records: list[dict]) -> dict:
        """把 scope 剧情历史整体覆盖为 records(分支切换),快照重新去重。"""
        async with self._get_lock(scope):
            def _overwrite() -> tuple[int, int]:
                old = self._load_history(scope)
                table = {k: [] for k in SNAP_KEYS}
                new_records: list[dict] = []
                for r in records:
                    if not r.get("id"):
                        continue
                    ref = self._build_ref(r, table)
                    nr = dict(r)
                    nr["scope"] = scope
                    nr["_ref"] = ref
                    # 展开的输入里通常带着快照字段,收敛进版本表后移除,保持单文件精简
                    nr = self._strip_snap(nr)
                    new_records.append(nr)
                data = {"records": new_records, "versions": table}
                self._save_history(scope, data)
                return len(new_records), len(old["records"]) - len(new_records)

            written, deleted = await asyncio.to_thread(_overwrite)
            return {"written": written, "deleted": deleted}

    # ─── 分支历史(与 history.json 同目录) ─────────────────────

    @staticmethod
    def _encode_branch(name: str) -> str:
        return quote_plus(name, safe="")

    @staticmethod
    def _decode_branch(stem: str) -> str:
        return unquote_plus(stem)

    def _branch_path(self, scope: str, branch_name: str) -> str:
        safe = self._encode_branch(branch_name)
        return os.path.join(self._scope_dir(scope), f"{BRANCH_FILE_PREFIX}{safe}.json")

    async def save_branch_history(self, scope: str, branch_name: str) -> bool:
        """把当前剧情历史整体归档为分支历史文件(同目录,结构同 history.json)。

        分支文件与 history.json 同构(records + versions),versions 自洽 ——
        切换分支时直接整个文件复制回 history.json 即可,无需重建版本表。
        返回 True;当前无任何记录时也照样归档(空分支)。
        """
        async with self._get_lock(scope):
            def _run() -> bool:
                data = self._load_history(scope)
                snapshot = {
                    "_format": 2,
                    "branch": branch_name,
                    "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "versions": data["versions"],
                    "records": data["records"],
                }
                write_json_atomic(self._branch_path(scope, branch_name), snapshot)
                return True

            return await asyncio.to_thread(_run)

    async def load_branch_history(self, scope: str, branch_name: str) -> dict | None:
        """读某分支的历史文件(展开后的 records + versions),缺返回 None。"""
        def _run():
            data = read_json(self._branch_path(scope, branch_name))
            if not isinstance(data, dict):
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

    async def switch_to_branch(self, scope: str, branch_name: str) -> bool:
        """切换到某分支:把该分支的历史文件整体复制为 history.json。

        versions 表随文件一起复制,记录里的 _ref 指向同一份 versions,天然自洽,
        无需重建去重表。分支文件不存在时返回 False(调用方回退 overwrite_all)。
        """
        async with self._get_lock(scope):
            def _run() -> bool:
                path = self._branch_path(scope, branch_name)
                data = read_json(path)
                if not isinstance(data, dict) or "records" not in data:
                    return False
                # 抹掉归档元字段,落成标准 history.json
                clean = {"records": data.get("records") or [], "versions": data.get("versions") or {}}
                self._save_history(scope, clean)
                return True

            return await asyncio.to_thread(_run)

    async def delete_branch_history(self, scope: str, branch_name: str) -> bool:
        """删除某分支的历史文件。"""
        return await asyncio.to_thread(safe_remove, self._branch_path(scope, branch_name))

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
                stem = f[len(BRANCH_FILE_PREFIX) : -5]
                try:
                    name = self._decode_branch(stem)
                except ValueError:
                    continue
                d = read_json(os.path.join(scope_dir, f))
                out[name] = {
                    "saved_at": (d or {}).get("saved_at", ""),
                    "record_count": len((d or {}).get("records") or []),
                }
            return out

        return await asyncio.to_thread(_run)