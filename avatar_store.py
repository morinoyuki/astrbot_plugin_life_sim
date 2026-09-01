"""角色头像管理器。

负责把用户通过指令上传的角色头像图片保存到 data 目录下的 ``avatars/`` 子目录,
并在渲染聊天卡片时按角色名查询。

目录结构(按 scope 分区,群聊/私聊头像彼此隔离)::

    <data_dir>/
      avatars/
        default.png                  # 全局默认头像(可选):所有 scope 未设置专属头像的角色都会用它
        group_<gid>/
          <角色名>.png               # 群聊头像
        user_<uid>/
          <角色名>.png               # 私聊头像

每个方法都接受 `scope` 参数(形如 ``group_<gid>`` / ``user_<uid>``,由调用方提供):
- ``scope`` 为空 → 操作根目录(全局,老布局)。
- ``scope`` 非空 → 操作 ``avatars/<scope>/`` 子目录。
- 默认头像 ``default.*`` 恒在根目录,作为所有 scope 的全局兜底。

头部分级:专属头像 > 默认头像(avatars/default.*) > 角色名首字符占位头像(渲染层绘制)。

并发安全:进程内加锁,同名单次只允许一个写入。文件按原子写(先写临时文件再 rename)。
"""

from __future__ import annotations

import hashlib
import io
import os
import threading
from urllib.parse import quote, unquote

from PIL import Image

# 允许的图片扩展名
_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
# 允许的图片 MIME 前缀
_ALLOWED_MIME = ("image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp")

# 默认头像的角色名:放在 avatars/ 根目录下,所有未设置专属头像的角色都会回退到它。
DEFAULT_AVATAR_NAME = "default"


def _sanitize_scope(scope: str) -> str:
    """把 scope 净化为安全的子目录名(杜绝路径穿越)。

    scope 形如 ``group_<digits>`` / ``user_<digits>``(插件统一生成),这里是
    防御性净化:非法字符替换为 ``_``,空串 / 纯路径元字符返回空(代表根目录)。
    """
    if not scope:
        return ""
    cleaned = str(scope).replace("/", "_").replace("\\", "_").strip().rstrip(".:")
    if not cleaned or cleaned in (".", ".."):
        return ""
    return cleaned[:64]


class AvatarStore:
    """角色头像存取。线程安全,所有方法都返回可 JSON 序列化的普通值。"""

    def __init__(self, data_dir: str, subdir: str = "avatars") -> None:
        self.base = os.path.join(data_dir, subdir)
        os.makedirs(self.base, exist_ok=True)
        # 名字 → 已计算路径缓存(进程内)[name] = abs path or None
        self._locks: dict[str, threading.Lock] = {}
        self._lock_guard = threading.Lock()

    # ── 内部 ──────────────────────────────────────────────────
    def _lock(self, name: str) -> threading.Lock:
        with self._lock_guard:
            lk = self._locks.get(name)
            if lk is None:
                lk = threading.Lock()
                self._locks[name] = lk
            return lk

    @staticmethod
    def _sanitize(name: str) -> str:
        """规范化角色名:去空格、去路径分隔符,限制长度。"""
        name = (name or "").strip().rstrip('\\/:*?"<>|')
        return name[:64] if name else ""

    @staticmethod
    def _filename(name: str, ext: str = ".png") -> str:
        """角色名 → 安全的文件名(URL 编码防止路径穿越)。"""
        token = quote(name, safe="")
        return f"{token}{ext}"

    def _scope_dir(self, scope: str) -> str:
        """scope 对应的子目录(空 scope → 根目录),不存在时创建。"""
        safe = _sanitize_scope(scope)
        if not safe:
            return self.base
        d = os.path.join(self.base, safe)
        os.makedirs(d, exist_ok=True)
        return d

    def _path_for(self, name: str, scope: str = "") -> str:
        return os.path.join(self._scope_dir(scope), self._filename(name))

    def _resolved_paths(self, name: str, scope: str = "") -> list[str]:
        """返回指定 scope 的候选路径 + 根目录候选路径(兼容老布局回退)。"""
        target = self._filename(name, "")
        dirs = [self._scope_dir(scope)] if scope else []
        dirs.append(self.base)
        out: list[str] = []
        seen: set[str] = set()
        for d in dirs:
            if not os.path.isdir(d):
                continue
            for p in os.listdir(d):
                if not p.startswith(target) or p in seen:
                    continue
                full = os.path.join(d, p)
                if os.path.isfile(full) and any(
                    p.endswith(e) for e in _ALLOWED_EXT
                ):
                    out.append(full)
                    seen.add(full)
        return out

    # ── 写 ────────────────────────────────────────────────────
    def save_avatar(
        self,
        name: str,
        image_bytes: bytes,
        mime: str = "image/png",
        scope: str = "",
    ) -> str | None:
        """保存角色头像(默认存到 scope 子目录;scope 为空存根目录)。

        Returns:
            成功返回保存后的绝对路径;失败(非法名字/空数据/非图片)返回 None。
        """
        name = self._sanitize(name)
        if not name:
            return None
        if not image_bytes:
            return None

        ext = self._guess_ext(mime)
        path = self._path_for(name, scope)

        with self._lock(name):
            tmp = ""
            try:
                img = Image.open(io.BytesIO(image_bytes))
                img.verify()  # 校验完整性
            except Exception:
                return None
            try:
                img = Image.open(io.BytesIO(image_bytes))
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA")
                # 原子写
                tmp = path + f".{os.getpid()}.tmp"
                img.save(tmp, "PNG")
                os.replace(tmp, path)
                return path
            except Exception:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                return None
        # ext 变量保持引用,避免 linter 告警
        _ = ext

    def save_avatar_from_image(
        self, name: str, image: Image.Image, scope: str = ""
    ) -> str | None:
        """从 PIL Image 保存。"""
        name = self._sanitize(name)
        if not name:
            return None
        buf = io.BytesIO()
        try:
            image.convert("RGBA").save(buf, "PNG")
        except Exception:
            return None
        return self.save_avatar(name, buf.getvalue(), "image/png", scope=scope)

    # ── 读 ────────────────────────────────────────────────────
    def get_avatar(self, name: str, scope: str = "") -> str | None:
        """按角色名查询头像绝对路径。

        优先在该 scope 内精确匹配;scope 内没有时回退根目录(老布局头像)。
        不存在返回 None。
        """
        name = self._sanitize(name)
        if not name or name == DEFAULT_AVATAR_NAME:
            return None
        paths = self._resolved_paths(name, scope)
        return paths[0] if paths else None

    def get_default_avatar(self) -> str | None:
        """返回全局默认头像路径 avatars/default.*(在根目录,常驻)。"""
        prefix = self._filename(DEFAULT_AVATAR_NAME, "")
        for f in os.listdir(self.base):
            full = os.path.join(self.base, f)
            if f.startswith(prefix) and os.path.isfile(full):
                return full
        return None

    def resolve(self, name: str, scope: str = "") -> str | None:
        """解析角色头像,带默认头像回退。优先级:scope 专属(或老布局) > 全局默认。"""
        own = self.get_avatar(name, scope)
        if own:
            return own
        return self.get_default_avatar()

    def delete(self, name: str, scope: str = "") -> bool:
        """删除角色头像(scope 专属;scope 为空删根目录)。返回是否删除成功。"""
        name = self._sanitize(name)
        if not name:
            return False
        with self._lock(name):
            path = self.get_avatar(name, scope)
            if not path:
                return False
            try:
                os.remove(path)
                return True
            except OSError:
                return False

    def list_names(self, scope: str = "") -> list[str]:
        """返回该 scope 下所有已保存头像的角色名(排序)。"""
        result: list[str] = []
        scope_dir = self._scope_dir(scope) if scope else self.base
        if not os.path.isdir(scope_dir):
            return result
        for f in sorted(os.listdir(scope_dir)):
            p = os.path.join(scope_dir, f)
            if os.path.isfile(p) and any(
                f.endswith(e) for e in _ALLOWED_EXT
            ):
                stem = os.path.splitext(f)[0]
                if stem == DEFAULT_AVATAR_NAME:
                    continue  # 默认头像不算角色
                try:
                    result.append(unquote(stem))
                except ValueError:
                    continue
        return result

    def clear_scope(self, scope: str) -> int:
        """删除该 scope 下的全部角色头像(不含根目录全局默认头像),返回删除数。"""
        safe = _sanitize_scope(scope)
        if not safe:
            return 0
        d = os.path.join(self.base, safe)
        if not os.path.isdir(d):
            return 0
        count = 0
        for f in os.listdir(d):
            p = os.path.join(d, f)
            if os.path.isfile(p) and any(f.endswith(e) for e in _ALLOWED_EXT):
                try:
                    os.remove(p)
                    count += 1
                except OSError:
                    pass
        try:
            os.rmdir(d)
        except OSError:
            pass
        return count

    # ── 工具 ──────────────────────────────────────────────────
    @staticmethod
    def _guess_ext(mime: str) -> str:
        m = (mime or "").lower()
        if "jpeg" in m:
            return ".jpg"
        if "webp" in m:
            return ".webp"
        if "gif" in m:
            return ".gif"
        if "bmp" in m:
            return ".bmp"
        if "png" in m:
            return ".png"
        return ".png"

    @staticmethod
    def md5(data: bytes) -> str:
        return hashlib.md5(data).hexdigest()