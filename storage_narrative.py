"""剧情历史存档(独立于 sim session 存储)。

按 `scope` 分目录存放,每条记录一个 JSON 文件:
    <data_dir>/narrative_history/<scope>/<record_id>.json

`scope` 由调用方提供,形如 `group_<gid>` 或 `user_<uid>`(与 `_sim_session_key` 同源),
保证不同群/私聊的历史彼此隔离。

记录字段:
    id                - 短 ID,形如 `n_<8位hex>`
    scope             - 写入时所属 scope
    source_session_key - 关联 sim session key
    user_action       - 触发本段的用户输入(已剥 <system_reminder> / <Quoted Message>)
    summary           - 自动摘要(首段标题或前 50 字)
    narrative         - 完整剧情文本(可被 revise 覆盖)
    created_at        - ISO 时间
    revised_at        - ISO 时间,初次与 created_at 一致

快照字段去重:
    `world_setting / character_lore / world_lore` 三字段在同一 scope 内连续轮次常
    完全重复(实测 206 条只有 1 / 67 / 23 种内容)。为避免每条记录整份拷贝,每个
    scope 共享版本表 `_versions.json`(含三个并行数组,按内容寻址)。记录本体只存
    `_ref` 三个整数索引;读取(list / get / overwrite_all)时按 `_ref` 透明还原三字段。

    旧记录(无 `_ref`)原样返回,兼容老数据;新记录写入时自动去重。

API:
    append(scope, payload)         - 写入并返回 id
    revise(scope, id, narrative)   - 覆盖 narrative + revised_at
    get(scope, id)                 - 读单条(缺 None)
    list(scope)                    - 列本 scope 全部(按 created_at 升序,快照已还原)
    list_all_for_owner(owner_uid)  - 列此用户相关所有 scope 的记录(跨群)
    delete(scope, id)              - 删一条
    delete_scope(scope)            - 清空整个 scope 目录(含版本表)
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time

from .storage_base import (
    ensure_dir,
    read_json,
    safe_remove,
    sanitize_key,
    write_json_atomic,
)

SUB_DIR = "narrative_history"
VERSIONS_FILE = "_versions.json"
# 参与去重的三字段(顺序对齐 `_ref` 索引)
SNAP_KEYS = ("world_setting", "character_lore", "world_lore")


def _gen_id() -> str:
    return "n_" + secrets.token_hex(4)


class NarrativeStore:
    """剧情历史存储封装(独立于 sim session)。"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._root = ensure_dir(os.path.join(data_dir, SUB_DIR))
        # scope → 版本表(进程内缓存,避免每次读盘)
        self._ver_cache: dict[str, dict] = {}

    # ── 内部路径 ─────────────────────────────────────────────
    def _scope_dir(self, scope: str) -> str:
        safe_scope = sanitize_key(scope)
        if not safe_scope:
            raise ValueError("scope 不能为空")
        return ensure_dir(os.path.join(self._root, safe_scope))

    def _path(self, scope: str, record_id: str) -> str:
        safe_id = sanitize_key(record_id)
        if not safe_id:
            raise ValueError("record_id 不能为空")
        return os.path.join(self._scope_dir(scope), f"{safe_id}.json")

    def _versions_path(self, scope: str) -> str:
        return os.path.join(self._scope_dir(scope), VERSIONS_FILE)

    def list_scopes(self) -> list[str]:
        if not os.path.exists(self._root):
            return []
        return [
            d
            for d in os.listdir(self._root)
            if os.path.isdir(os.path.join(self._root, d))
        ]

    # ── 版本表 ───────────────────────────────────────────────
    def _load_versions(self, scope: str) -> dict:
        cached = self._ver_cache.get(scope)
        if cached is not None:
            return cached
        data = read_json(self._versions_path(scope))
        if not isinstance(data, dict):
            data = {}
        table = {}
        for k in SNAP_KEYS:
            v = data.get(k, [])
            table[k] = v if isinstance(v, list) else []
        self._ver_cache[scope] = table
        return table

    def _save_versions(self, scope: str, table: dict) -> None:
        self._ver_cache[scope] = table
        write_json_atomic(self._versions_path(scope), table)

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
    def _strip_snap(payload: dict) -> dict:
        return {k: v for k, v in payload.items() if k not in SNAP_KEYS}

    # ── 写入 ───────────────────────────────────────────────

    async def append(self, scope: str, payload: dict) -> str:
        """写入一条新记录,返回 record_id。快照字段自动去重进版本表。"""
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        record_id = _gen_id()

        def _write():
            table = self._load_versions(scope)
            ref = self._build_ref(payload, table)
            record = {
                "id": record_id,
                "scope": scope,
                "created_at": now,
                "revised_at": now,
                "_ref": ref,
                **self._strip_snap(payload),
            }
            self._save_versions(scope, table)
            write_json_atomic(self._path(scope, record_id), record)

        await asyncio.to_thread(_write)
        return record_id

    async def revise(self, scope: str, record_id: str, narrative: str) -> bool:
        """只覆盖 narrative + revised_at;其它字段(含 _ref / 快照)保留。"""
        path = self._path(scope, record_id)
        existing = await asyncio.to_thread(read_json, path)
        if existing is None:
            return False
        existing["narrative"] = narrative
        existing["revised_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        existing["revised_count"] = int(existing.get("revised_count", 0)) + 1
        await asyncio.to_thread(write_json_atomic, path, existing)
        return True

    async def restore(self, scope: str, state: dict) -> bool:
        """就地恢复 narrative / revised_count / revised_at(/undo 回滚)。快照不动。"""
        record_id = state.get("id")
        if not record_id:
            return False
        path = self._path(scope, record_id)
        existing = await asyncio.to_thread(read_json, path)
        if existing is None:
            return False
        existing["narrative"] = state.get("narrative", existing.get("narrative", ""))
        existing["revised_count"] = int(state.get("revised_count", 0))
        existing["revised_at"] = state.get("revised_at", existing.get("revised_at", ""))
        await asyncio.to_thread(write_json_atomic, path, existing)
        return True

    # ─── 读取 ───────────────────────────────────────────────

    async def get(self, scope: str, record_id: str) -> dict | None:
        def _run():
            data = read_json(self._path(scope, record_id))
            if not data:
                return None
            return self._expand_record(data, self._load_versions(scope))

        return await asyncio.to_thread(_run)

    async def list(self, scope: str) -> list[dict]:
        """列本 scope 全部记录,按 created_at 升序,快照字段还原。"""
        scope_dir = self._scope_dir(scope)
        if not os.path.exists(scope_dir):
            return []

        def _load_all():
            table = self._load_versions(scope)
            out: list[dict] = []
            for fname in os.listdir(scope_dir):
                if not fname.endswith(".json"):
                    continue
                if fname == VERSIONS_FILE:
                    continue
                data = read_json(os.path.join(scope_dir, fname))
                if data:
                    out.append(self._expand_record(data, table))
            out.sort(key=lambda r: (r.get("created_at", ""), r.get("id", "")))
            return out

        return await asyncio.to_thread(_load_all)

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
        return await asyncio.to_thread(safe_remove, self._path(scope, record_id))

    async def delete_scope(self, scope: str) -> int:
        """删整个 scope 目录下的所有记录 + 版本表文件,返回删除条数。"""
        scope_dir = self._scope_dir(scope)
        if not os.path.exists(scope_dir):
            return 0

        def _purge():
            count = 0
            for fname in os.listdir(scope_dir):
                if not fname.endswith(".json"):
                    continue
                if safe_remove(os.path.join(scope_dir, fname)):
                    count += 1
            try:
                os.rmdir(scope_dir)
            except OSError:
                pass
            self._ver_cache.pop(scope, None)
            return count

        return await asyncio.to_thread(_purge)

    async def overwrite_all(self, scope: str, records: list[dict]) -> dict:
        """把 scope 剧情历史整体覆盖为 records(分支切换),快照字段重新去重。"""
        target_ids = {r.get("id") for r in records if r.get("id")}
        scope_dir = self._scope_dir(scope)

        def _overwrite() -> tuple[int, int]:
            table = {k: [] for k in SNAP_KEYS}
            deleted = 0
            if os.path.exists(scope_dir):
                for fname in os.listdir(scope_dir):
                    if not fname.endswith(".json") or fname == VERSIONS_FILE:
                        continue
                    if fname[:-5] not in target_ids and safe_remove(
                        os.path.join(scope_dir, fname)
                    ):
                        deleted += 1
            written = 0
            for r in records:
                rid = r.get("id")
                if not rid:
                    continue
                ref = self._build_ref(r, table)
                record = dict(r)
                record["scope"] = scope
                record["_ref"] = ref
                write_json_atomic(self._path(scope, rid), record)
                written += 1
            self._save_versions(scope, table)
            return written, deleted

        written, deleted = await asyncio.to_thread(_overwrite)
        return {"written": written, "deleted": deleted}