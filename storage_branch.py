"""剧情分支快照存储(独立于 sim session)。

每个 scope(群/私聊)一个目录,每个分支一个 JSON 文件:
    <data_dir>/sim_branches/<scope>/<urlencoded_name>.json

分支是自包含的完整快照(messages / lore / RPG / 剧情历史),不与会话文件耦合;
会话 /创建 /删除 时由调用方显式清理本 scope 的分支目录。

分支名编码:
    - 文件名 = quote_plus(name, safe="")  (双向可逆,杜绝路径分隔符/冒号等非法字符)
    - 真实名存在分支 dict 的 "name" 字段里,list() 时读回
    - 保留名 "主线" 也走同一编码,无特殊处理

API 设计:
    save(scope, name, data)   -> 写入(覆盖)
    get(scope, name)          -> dict | None
    delete(scope, name)       -> bool
    list(scope)               -> dict[str, dict]  (real_name -> branch)
    delete_scope(scope)       -> int  (删除整个 scope 的所有分支)
    scope_exists(scope)       -> bool

IO 走 `asyncio.to_thread`,与 SimStore / NarrativeStore 保持一致,不阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import os
import time
from urllib.parse import quote_plus, unquote_plus

from astrbot.api import logger

from .storage_base import (
    ensure_dir,
    read_json,
    safe_remove,
    sanitize_key,
    write_json_atomic,
)

SUB_DIR = "sim_branches"


def _encode_name(name: str) -> str:
    """把分支名编码为安全的文件名 stem。

    quote_plus 把 UTF-8 字节转成 %XX 格式,空格变 `+`,双向可逆。
    极端情况:文件名最长 255 字节,分支名 ≤ 30 字符 → 最大 ~90 字节,安全。
    """
    return quote_plus(name, safe="")


def _decode_name(stem: str) -> str:
    """从文件名 stem 还原分支名。"""
    return unquote_plus(stem)


class BranchStore:
    """剧情分支快照存储。"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._root = ensure_dir(os.path.join(data_dir, SUB_DIR))

    # ─── 路径 ──────────────────────────────────────────────────

    def _scope_dir(self, scope: str) -> str:
        safe = sanitize_key(scope)
        return ensure_dir(os.path.join(self._root, safe))

    def _path(self, scope: str, name: str) -> str:
        return os.path.join(self._scope_dir(scope), f"{_encode_name(name)}.json")

    # ─── 写 ─────────────────────────────────────────────────────

    async def save(self, scope: str, name: str, data: dict) -> None:
        """写入分支快照(覆盖同名分支)。data 会被注入 name 和 saved_at 字段。"""
        record = dict(data)
        record["name"] = name
        record["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

        def _write():
            write_json_atomic(self._path(scope, name), record)

        await asyncio.to_thread(_write)

    # ─── 读 ─────────────────────────────────────────────────────

    async def get(self, scope: str, name: str) -> dict | None:
        """读取单条分支快照。"""
        return await asyncio.to_thread(read_json, self._path(scope, name))

    async def list(self, scope: str) -> dict[str, dict]:
        """列出 scope 下所有分支,返回 {真实名: branch_dict}。

        跳过损坏/缺失文件。返回的 dict 按键名的保存顺序排序(文件系统顺序)。
        """
        scope_dir = os.path.join(self._root, sanitize_key(scope))

        def _list_all():
            if not os.path.exists(scope_dir):
                return {}
            branches: list[dict] = []
            for fname in os.listdir(scope_dir):
                if not fname.endswith(".json"):
                    continue
                stem = fname[:-5]
                branch = read_json(os.path.join(scope_dir, fname))
                if branch is None:
                    logger.warning(
                        f"life-sim: 分支快照文件损坏,已跳过: {scope}/{fname}"
                    )
                    continue
                branch["_name"] = branch.get("name") or _decode_name(stem)
                branches.append(branch)
            # 按保存时间升序显示,同秒按名字兜底(保证稳定)
            branches.sort(
                key=lambda b: (b.get("saved_at", ""), str(b.get("_name", "")))
            )
            return {b.pop("_name"): b for b in branches}

        return await asyncio.to_thread(_list_all)

    async def scope_exists(self, scope: str) -> bool:
        """scope 目录下是否有分支文件。"""
        scope_dir = os.path.join(self._root, sanitize_key(scope))

        def _exists():
            if not os.path.exists(scope_dir):
                return False
            return any(f.endswith(".json") for f in os.listdir(scope_dir))

        return await asyncio.to_thread(_exists)

    # ─── 删 ─────────────────────────────────────────────────────

    async def delete(self, scope: str, name: str) -> bool:
        """删除单条分支快照。"""
        return await asyncio.to_thread(
            safe_remove, self._path(scope, name)
        )

    async def delete_scope(self, scope: str) -> int:
        """删除整个 scope 的所有分支快照,返回删除条数。"""
        scope_dir = os.path.join(self._root, sanitize_key(scope))

        def _purge():
            if not os.path.exists(scope_dir):
                return 0
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