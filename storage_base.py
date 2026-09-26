"""JSON 文件存储公共原语。

sim 和 rpg 的存储都基于"一个 key 一个 .json 文件"的模型,共享这些原语:
- 原子写(tmp + replace)
- 容错读(损坏 / 缺失 → None)
- 静默删除(FileNotFoundError 不当异常)
- makedirs
- 枚举目录下所有 .json 的 stem

调用方按自己的目录 / 命名约定拼出 `path` 再传入这些函数即可,不在这里封类。
"""

from __future__ import annotations

import json
import os
import uuid

from astrbot.api import logger


def read_json(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("存档损坏 %s: %s", path, e)
        return None
    # 顶层必须是对象:容忍旧版/手工编辑写成了数组或标量,
    # 否则调用方可能在 list 上做 char[key] = ... 直接崩(如 rpg load_char)
    if not isinstance(data, dict):
        logger.warning("存档格式非对象,已忽略 %s: %s", path, type(data).__name__)
        return None
    return data


def write_json_atomic(path: str, data: dict) -> None:
    # 临时文件名必须唯一:固定 path + ".tmp" 在并发写同一路径时会互相截断,
    # 最终 os.replace 只保留其中一个写入者的(可能交错的)内容。
    tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError) as e:
        logger.warning("存档写入失败 %s: %s", path, e)
        try:
            os.remove(tmp)
        except OSError:
            pass


def safe_remove(path: str) -> bool:
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        logger.warning("存档删除失败 %s: %s", path, e)
        return False


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def list_json_stems(dir_path: str) -> list[str]:
    if not os.path.exists(dir_path):
        return []
    return [f[:-5] for f in os.listdir(dir_path) if f.endswith(".json")]


def sanitize_key(key: str) -> str:
    """把任意 key 净化为可作为文件名的安全 stem。

    拒绝:
    - 空字符串 / 纯空白
    - 含路径分隔符 / `..` / 控制字符的 key

    所有非法字符统一替换为 `_`,首尾空白剥离。
    """
    if not isinstance(key, str):
        raise TypeError(f"key 必须是 str,收到 {type(key).__name__}")
    cleaned = key.replace("/", "_").replace("\\", "_").strip()
    if not cleaned or cleaned == "." or cleaned == "..":
        raise ValueError("key 不能为空或仅由路径元字符组成")
    if "\x00" in cleaned or any(ord(c) < 32 for c in cleaned):
        raise ValueError("key 不能含控制字符")
    return cleaned
