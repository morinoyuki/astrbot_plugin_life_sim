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
    world_setting     - 写入时的世界设定快照(用户后期 /创建 新会话不会影响老记录)
    character_lore    - 写入时的角色设定快照
    world_lore        - 写入时的世界观信息快照
    created_at        - ISO 时间
    revised_at        - ISO 时间,初次与 created_at 一致

API:
    append(scope, payload)         - 写入并返回 id
    revise(scope, id, narrative)   - 覆盖 narrative + revised_at
    get(scope, id)                 - 读单条(缺 None)
    list(scope)                    - 列本 scope 全部(按 created_at 升序)
    list_all_for_owner(owner_uid)  - 列此用户相关所有 scope 的记录(跨群)
    delete(scope, id)              - 删一条
    delete_scope(scope)            - 清空整个 scope 目录
"""

from __future__ import annotations

import asyncio
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


def _gen_id() -> str:
    """生成 `n_<8hex>` 短 ID,冲突概率极低(单 scope 内 <10k 条时)。"""
    return "n_" + secrets.token_hex(4)


class NarrativeStore:
    """剧情历史存储封装(独立于 sim session)。"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._root = ensure_dir(os.path.join(data_dir, SUB_DIR))

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

    def list_scopes(self) -> list[str]:
        if not os.path.exists(self._root):
            return []
        return [
            d
            for d in os.listdir(self._root)
            if os.path.isdir(os.path.join(self._root, d))
        ]

    # ─── 写入 ───────────────────────────────────────────────

    async def append(self, scope: str, payload: dict) -> str:
        """写入一条新记录,返回分配的 record_id。

        payload 应包含 narrative(必填)、user_action / summary / world_setting /
        character_lore / world_lore / source_session_key(可选)。
        创建时间与首次 revised_at 自动填入。
        """
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        record_id = _gen_id()
        record = {
            "id": record_id,
            "scope": scope,
            "created_at": now,
            "revised_at": now,
            **payload,
        }
        await asyncio.to_thread(
            write_json_atomic,
            self._path(scope, record_id),
            record,
        )
        return record_id

    async def revise(self, scope: str, record_id: str, narrative: str) -> bool:
        """只覆盖 narrative 字段 + revised_at;其它字段(包括 world_setting 快照)保留。"""
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
        """就地恢复单条记录的 narrative / revised_count / revised_at(用于 /undo 回滚)。

        world_setting / character_lore / world_lore 等快照字段**不动** — 它们是历史信息,
        撤销叙事不该篡改记录写入时刻的世界观快照。

        缺失的记录(已被外部删除)直接返回 False。
        """
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
        return await asyncio.to_thread(read_json, self._path(scope, record_id))

    async def list(self, scope: str) -> list[dict]:
        """列本 scope 全部记录,按 created_at 升序。"""
        scope_dir = self._scope_dir(scope)
        if not os.path.exists(scope_dir):
            return []

        def _load_all():
            out: list[dict] = []
            for fname in os.listdir(scope_dir):
                if not fname.endswith(".json"):
                    continue
                data = read_json(os.path.join(scope_dir, fname))
                if data:
                    out.append(data)
            # created_at 只到秒,同秒内用 id 作 tie-breaker 保证稳定顺序
            # (ID 含 secrets.token_hex,无序;但能保证全序)
            out.sort(key=lambda r: (r.get("created_at", ""), r.get("id", "")))
            return out

        return await asyncio.to_thread(_load_all)

    async def list_all_for_owner(
        self, sender_uid: str, current_scope: str = ""
    ) -> list[dict]:
        """列 sender 可见的所有 scope 记录。

        可见性规则(避免跨用户隐私泄露):
        - `user_<uid>` 私聊 scope:仅 owner 可见(uid 必须等于 sender_uid)
        - `group_<gid>` 群聊 scope:全员可见(已通过 AstrBot 鉴权,本插件不做成员校验)
        - 其它 scope 命名:保守跳过

        `current_scope` 兜底:即使命名不匹配,也允许它(调用方一般就是从那里来的)。
        """
        out: list[dict] = []
        sender_uid = (sender_uid or "").strip()
        for scope in self.list_scopes():
            if scope.startswith("user_"):
                # 私聊只能看自己的(若 current_scope 就是这个,放行)
                if scope != current_scope and scope != f"user_{sender_uid}":
                    continue
            elif scope.startswith("group_"):
                pass  # 群聊全员可见
            else:
                # 未知 scope 命名 — 仅当正好是 current_scope 时放行
                if scope != current_scope:
                    continue
            out.extend(await self.list(scope))
        out.sort(key=lambda r: r.get("created_at", ""))
        return out

    # ─── 删除 ───────────────────────────────────────────────

    async def delete(self, scope: str, record_id: str) -> bool:
        return await asyncio.to_thread(safe_remove, self._path(scope, record_id))

    async def delete_scope(self, scope: str) -> int:
        """删整个 scope 目录下的所有记录,返回删除条数。"""
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
            return count

        return await asyncio.to_thread(_purge)

    async def overwrite_all(self, scope: str, records: list[dict]) -> dict:
        """把 scope 的剧情历史**整体覆盖**为 records(用于分支切换)。

        - 目标记录(id 在 records 里)直接重写为最新内容(含所有快照字段)
        - 磁盘上存在但不在 records 里的记录删除

        返回 {"written": int, "deleted": int}。
        """
        target_ids = {r.get("id") for r in records if r.get("id")}
        scope_dir = self._scope_dir(scope)

        def _overwrite() -> tuple[int, int]:
            deleted = 0
            if os.path.exists(scope_dir):
                for fname in os.listdir(scope_dir):
                    if not fname.endswith(".json"):
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
                record = dict(r)
                record["scope"] = scope
                write_json_atomic(self._path(scope, rid), record)
                written += 1
            return written, deleted

        written, deleted = await asyncio.to_thread(_overwrite)
        return {"written": written, "deleted": deleted}
