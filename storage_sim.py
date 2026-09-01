"""转生模拟器会话存储。

每个 sim session 一个 JSON 文件:`<data_dir>/sim_sessions/<key>.json`
- 写盘走 `storage_base.write_json_atomic`(tmp + replace,崩在中途也不留半截)
- 同步 IO 通过 `asyncio.to_thread` 包到线程池,不阻塞事件循环
- key 由调用方提供(形如 `group_<gid>` / `user_<uid>`),路径层做一次净化
"""

from __future__ import annotations

import asyncio
import os

from .storage_base import (
    ensure_dir,
    read_json,
    safe_remove,
    sanitize_key,
    write_json_atomic,
)

SUB_DIR = "sim_sessions"


class SimStore:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._dir = ensure_dir(os.path.join(data_dir, SUB_DIR))

    def _path(self, key: str) -> str:
        safe = sanitize_key(key)
        if not safe:
            raise ValueError("sim session key 不能为空")
        return os.path.join(self._dir, f"{safe}.json")

    async def load(self, key: str) -> dict | None:
        path = self._path(key)
        return await asyncio.to_thread(read_json, path)

    async def save(self, key: str, session: dict) -> None:
        path = self._path(key)
        await asyncio.to_thread(write_json_atomic, path, session)

    async def delete(self, key: str) -> None:
        path = self._path(key)
        await asyncio.to_thread(safe_remove, path)
