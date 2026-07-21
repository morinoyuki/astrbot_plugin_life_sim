"""RPG 存档与会话的文件存储。

目录布局:
    <data_dir>/rpg_saves/<uid>.json          # 角色存档
    <data_dir>/sessions/<session_id>.json    # 会话文件

类 API 替代原先散落的自由函数;调用方持有一个 `RpgStore` 实例,不直接拼路径。

清理群/私聊 scope 的逻辑在 `purge_group` 内:群聊按 `{group_id}_` 前缀过滤角色,
私聊按无下划线 + sender_uid 匹配。
"""

from __future__ import annotations

import os

from .storage_base import (
    ensure_dir,
    list_json_stems,
    read_json,
    safe_remove,
    write_json_atomic,
)
from astrbot.api import logger

CHARS_SUBDIR = "rpg_saves"
SESS_SUBDIR = "sessions"

_OLD_FIELD_MIGRATIONS: tuple[tuple[str, object], ...] = (
    ("world_rules", {}),
    ("world", "default"),
)


class RpgStore:
    """RPG 角色存档 + 会话文件的存储封装。"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._chars_dir = ensure_dir(os.path.join(data_dir, CHARS_SUBDIR))
        self._sess_dir = ensure_dir(os.path.join(data_dir, SESS_SUBDIR))

    # ─── 路径 / 枚举 ────────────────────────────────────────────

    def _char_path(self, uid: str) -> str:
        return os.path.join(self._chars_dir, f"{uid}.json")

    def _sess_path(self, session_id: str) -> str:
        return os.path.join(self._sess_dir, f"{session_id}.json")

    @property
    def chars_dir(self) -> str:
        return self._chars_dir

    @property
    def sessions_dir(self) -> str:
        return self._sess_dir

    def list_chars(self) -> list[str]:
        return list_json_stems(self._chars_dir)

    def list_sessions(self) -> list[str]:
        return list_json_stems(self._sess_dir)

    # ─── 角色存档 ──────────────────────────────────────────────

    def load_char(self, uid: str) -> dict | None:
        char = read_json(self._char_path(uid))
        if char is None:
            return None
        migrated = False
        for key, default in _OLD_FIELD_MIGRATIONS:
            if key not in char:
                char[key] = default
                migrated = True
        if migrated:
            self.save_char(uid, char)  # 失败无所谓,内存版本已完整
        return char

    def save_char(self, uid: str, char: dict) -> None:
        write_json_atomic(self._char_path(uid), char)

    def delete_char(self, uid: str) -> bool:
        return safe_remove(self._char_path(uid))

    # ─── 会话文件 ──────────────────────────────────────────────

    def load_session(self, session_id: str) -> dict | None:
        return read_json(self._sess_path(session_id))

    def save_session(self, session_id: str, data: dict) -> None:
        write_json_atomic(self._sess_path(session_id), data)

    def delete_session(self, session_id: str) -> bool:
        return safe_remove(self._sess_path(session_id))

    # ─── 群 / 私聊清理 ─────────────────────────────────────────

    def purge_group(self, group_id: str, sender_uid: str = "") -> dict:
        """删除指定群/私聊的 RPG 角色存档 + 会话文件。

        群聊(group_id 非空):删除 rpg_saves/ 里 `{group_id}_*.json` 的全部角色 +
            sessions/ 里 group_id 字段匹配的全部会话。
        私聊(group_id 为空):只清理 sender_uid 命名的角色(私聊的 char 文件名就是 sender_uid) +
            group_id 字段为空的会话(按 owner_uid 或 反查 members uid 兜底)。

        返回 {"deleted_chars": int, "deleted_sessions": [session_id, ...]}。
        """
        gid = (group_id or "").strip()
        uid = (sender_uid or "").strip()
        result = {"deleted_chars": 0, "deleted_sessions": []}

        prefix = f"{gid}_" if gid else ""
        for stem in self.list_chars():
            if gid:
                if not stem.startswith(prefix):
                    continue
            elif uid:
                # 私聊:仅删除文件名为 sender_uid 的角色(保留含下划线的其他存档)
                if stem != uid:
                    continue
            if safe_remove(self._char_path(stem)):
                result["deleted_chars"] += 1

        for sid in self.list_sessions():
            s = self.load_session(sid)
            if s is None:
                continue
            sess_group = (s.get("group_id") or "").strip()
            members = s.get("members") or []
            if gid:
                if sess_group != gid:
                    continue
            else:
                if sess_group:
                    continue
                if uid:
                    owner = (s.get("owner_uid") or "").strip()
                    # 私聊 char uid == sender_uid,需把 members 列表里
                    # 的角色名还原成私聊 uid(sender_uid)再比对
                    member_uids = {m for m in members}  # 私聊下 uid == name
                    if owner != uid and uid not in member_uids:
                        continue
            # 顺带删成员角色存档(防御性)
            for member_name in members:
                member_uid = (
                    f"{sess_group}_{member_name}" if sess_group else member_name
                )
                if safe_remove(self._char_path(member_uid)):
                    result["deleted_chars"] += 1
            if safe_remove(self._sess_path(sid)):
                result["deleted_sessions"].append(
                    s.get("session_id", sid)
                )
            else:
                logger.debug("rpg 会话删除失败 %s", sid)

        return result