"""角色头像管理器。

负责把用户通过指令上传的角色头像图片保存到 data 目录下的 ``avatars/`` 子目录,
并在渲染聊天卡片时按角色名查询。

目录结构::

    <data_dir>/
      avatars/
        default.png        # 全局默认头像(可选):未设置专属头像的角色都会用它
        <角色名>.png        # 单角色一个文件(名字 URL 编码)
        <角色名>.jpg

头部分级:专属头像 > 默认头像(avatas/default.*) > 内置人形剪影。

并发安全:进程内加锁,同名单次只允许一个写入。文件按原子写(先写临时文件再 rename)。
"""

from __future__ import annotations

import hashlib
import io
import os
import threading

from PIL import Image

# 允许的图片扩展名
_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
# 允许的图片 MIME 前缀
_ALLOWED_MIME = ("image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp")

# 默认头像的角色名:放在 avatars/ 目录下,所有未设置专属头像的角色都会回退到它。
# 例如 <data>/avatars/default.png
DEFAULT_AVATAR_NAME = "default"


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
        from urllib.parse import quote

        token = quote(name, safe="")
        return f"{token}{ext}"

    def _path_for(self, name: str) -> str:
        return os.path.join(self.base, self._filename(name))

    # ── 写 ────────────────────────────────────────────────────
    def save_avatar(
        self,
        name: str,
        image_bytes: bytes,
        mime: str = "image/png",
    ) -> str | None:
        """保存角色头像。

        Args:
            name: 角色名(去空格/去路径)。
            image_bytes: 原始图片字节。
            mime: MIME 类型,决定扩展名。

        Returns:
            成功返回保存后的绝对路径;失败(非法名字/空数据/非图片)返回 None。
        """
        name = self._sanitize(name)
        if not name:
            return None
        if not image_bytes:
            return None

        ext = self._guess_ext(mime)
        path = self._path_for(name)

        with self._lock(name):
            try:
                img = Image.open(io.BytesIO(image_bytes))
                img.verify()  # 校验完整性
            except Exception:
                return None
            # 统一转成 RGB / RGBA 的 PNG 落盘,避免格式兼容问题
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

    def save_avatar_from_image(self, name: str, image: Image.Image) -> str | None:
        """从 PIL Image 保存。"""
        name = self._sanitize(name)
        if not name:
            return None
        buf = io.BytesIO()
        try:
            image.convert("RGBA").save(buf, "PNG")
        except Exception:
            return None
        return self.save_avatar(name, buf.getvalue(), "image/png")

    # ── 读 ────────────────────────────────────────────────────
    def get_avatar(self, name: str) -> str | None:
        """按角色名查询头像绝对路径;不存在返回 None。"""
        name = self._sanitize(name)
        if not name or name == DEFAULT_AVATAR_NAME:
            return None
        # 精确匹配
        for p in os.listdir(self.base):
            if p.startswith(self._filename(name, "")):
                full = os.path.join(self.base, p)
                if os.path.isfile(full):
                    return full
        return None

    def get_default_avatar(self) -> str | None:
        """返回全局默认头像路径 avatars/<DEFAULT_AVATAR_NAME>.*(存在时)。"""
        prefix = self._filename(DEFAULT_AVATAR_NAME, "")
        for p in os.listdir(self.base):
            if p.startswith(prefix) and os.path.isfile(os.path.join(self.base, p)):
                return os.path.join(self.base, p)
        return None

    def resolve(self, name: str) -> str | None:
        """解析角色头像,带默认头像回退。

        返回优先级:角色专属头像(若有) → 全局默认头像 avatars/default.png(若有) → None。
        渲染层拿到 None 时再用内置剪影定格,实现「未上传头像时使用默认头像」。
        """
        own = self.get_avatar(name)
        if own:
            return own
        return self.get_default_avatar()

    def delete(self, name: str) -> bool:
        """删除角色头像。返回是否删除成功。"""
        name = self._sanitize(name)
        if not name:
            return False
        with self._lock(name):
            path = self.get_avatar(name)
            if not path:
                return False
            try:
                os.remove(path)
                return True
            except OSError:
                return False

    def list_names(self) -> list[str]:
        """返回所有已保存头像的角色名(排序)。"""
        result = []
        from urllib.parse import unquote

        for f in os.listdir(self.base):
            p = os.path.join(self.base, f)
            if os.path.isfile(p) and any(f.endswith(e) for e in _ALLOWED_EXT):
                stem = os.path.splitext(f)[0]
                if stem == DEFAULT_AVATAR_NAME:
                    continue  # 默认头像不算角色
                try:
                    result.append(unquote(stem))
                except ValueError:
                    continue  # 文件名不是合法 URL 编码,跳过
        return sorted(result)

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
