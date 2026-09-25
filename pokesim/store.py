"""队伍 / 对战状态的文件持久化(与 RPG 存档分离,按模拟会话 scope 分区)。

目录布局:
    <data_dir>/pokemon/<scope>.json

scope 通常等于 `_sim_session_key(event)`(群聊=群号,私聊=用户 id),
因此一个群/私聊共享同一支队伍,与「一个群同时只有一段人生」的设定一致。
"""

from __future__ import annotations

import json
import os
import re
import time

_SAFE = re.compile(r"[^\w.\-]+")


def _safe_name(scope: str) -> str:
    name = _SAFE.sub("_", str(scope or "")).strip("._")
    return name or "default"


class PokeStore:
    """宝可梦队伍/对战存档。单文件包含队伍、电脑、当前对战。"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._dir = os.path.join(data_dir, "pokemon")
        os.makedirs(self._dir, exist_ok=True)

    @property
    def directory(self) -> str:
        return self._dir

    def _path(self, scope: str) -> str:
        return os.path.join(self._dir, f"{_safe_name(scope)}.json")

    def load(self, scope: str) -> dict:
        path = self._path(scope)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("scope", scope)
                data.setdefault("party", [])
                data.setdefault("box", [])
                data.setdefault("battle", None)
                return data
        except (OSError, ValueError):
            pass
        return {
            "scope": scope,
            "party": [],
            "box": [],
            "battle": None,
            "updated_at": 0,
        }

    def save(self, scope: str, data: dict) -> None:
        data = dict(data)
        data["scope"] = scope
        data["updated_at"] = int(time.time())
        path = self._path(scope)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    def delete(self, scope: str) -> bool:
        path = self._path(scope)
        try:
            os.remove(path)
            return True
        except OSError:
            return False

    def list_scopes(self) -> list[str]:
        out = []
        try:
            for fn in os.listdir(self._dir):
                if fn.endswith(".json"):
                    out.append(fn[:-5])
        except OSError:
            pass
        return out
