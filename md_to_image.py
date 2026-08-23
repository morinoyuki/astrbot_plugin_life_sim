"""Markdown → 图片渲染(基于 pillowmd,无浏览器)。

整合自 astrbot_plugin_nobrowser_markdown_to_pic (Xican) 的核心逻辑,精简为:
- 一个转图开关(output_as_image)
- 模板目录(output_image_style_path,LoadMarkdownStyles 的自定义样式)
- 自动分页开关(output_image_auto_page)
- GIF 支持:样式自带多帧动画背景(elements.json 的 `page` + `duratio`)时,
  渲染结果 `imageType == "gif"`,这里按逐帧保存为 `.gif`(并带上帧时长)。

pillowmd 补丁在模块导入时自动应用(幂等、安全)。

临时文件:
- `md_render_to_path` 返回的临时文件由调用方负责最终清理(推荐交给
  `event.track_temporary_local_file`,由框架在事件处理完后统一删除)。
- 渲染 / 保存**失败**时,本模块会先删除已创建的临时文件再抛出,杜绝空文件残留。
"""

import asyncio
import dataclasses
import os
import re
import tempfile
from pathlib import Path

from astrbot.api import logger

# 必须在 import pillowmd 之前打补丁
from .pillowmd_patch import apply_patch as _apply_pillowmd_patch

_apply_pillowmd_patch(logger)

import pillowmd


def _clean_markdown_text(text: str) -> str:
    """清理 Markdown 文本:规范化代码块换行、归一化公式命令。"""
    text = re.sub(
        r"(\s*)```(?:\s*\n?)([\s\S]*?)(?:\n?\s*)```(\s*)",
        lambda m: f"\n```\n{m.group(2)}\n```\n",
        text,
        flags=re.DOTALL,
    )
    # pillowmd 不认 \dfrac / \tfrac,等效替换为 \frac
    text = re.sub(r"\\[dt]frac(?![A-Za-z])", r"\\frac", text)
    return text.strip()


def _remove_file(path: str | None) -> None:
    """静默删除一个临时文件(不存在 / 删除失败均不报错)。"""
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


class MdToImageMixin:
    """Markdown → 图片渲染 Mixin。需要主插件在 __init__ 调用 `_md_init()`。"""

    # 运行时属性(在 _md_init 赋值)
    _md_style: pillowmd.MdStyle | None = None
    _md_style_path: str = ""

    def _md_init(self) -> None:
        """初始化渲染引擎(样式惰性加载,首次渲染时读取模板目录)。"""
        self._md_style = None
        self._md_style_path = (self._cfg("output_image_style_path", "") or "").strip()

    async def _md_load_style(self) -> None:
        """惰性加载模板目录样式(LoadMarkdownStyles,含 setting.json + elements.json)。"""
        if self._md_style is not None or not self._md_style_path:
            return
        if not os.path.exists(self._md_style_path):
            logger.warning(
                f"life-sim: 样式路径不存在 {self._md_style_path},使用默认样式"
            )
            return
        try:
            loop = asyncio.get_running_loop()
            self._md_style = await loop.run_in_executor(
                None,
                lambda: pillowmd.LoadMarkdownStyles(self._md_style_path),
            )
            logger.info(f"life-sim: 已加载模板样式: {self._md_style_path}")
        except Exception as e:
            logger.warning(f"life-sim: 加载模板样式失败,使用默认样式: {e}")
            self._md_style = None

    def md_should_render(self, text: str) -> bool:
        """转图开关:开启即转图(不再按长度/正则判断)。"""
        return bool(self._cfg("output_as_image", False))

    @staticmethod
    def cleanup_render_file(path: str | None) -> None:
        """供调用方显式删除由 `md_render_to_path` 生成的临时文件。

        正常情况下调用方会用 `event.track_temporary_local_file` 交给框架统一清理;
        这里是对"已发送完、想立刻释放"场景的补充入口。幂等、可重复调用。
        """
        _remove_file(path)

    async def md_render_to_path(self, text: str, **kwargs) -> str:
        """渲染 Markdown 为图片,返回临时文件路径(.png 或 .gif)。

        GIF:样式带多帧动画背景时自动输出 `.gif`(逐帧保存 + 帧时长 + 无限循环)。
        kwargs 支持(透传给渲染器):
            title(str)       图片顶部标题
            autoPage(bool)   自动分页
            noDecoration(bool) 透明背景/无装饰

        失败路径内部自清理已创建的临时文件后抛出。
        """
        await self._md_load_style()
        cleaned = _clean_markdown_text(text)

        base_style = (
            self._md_style if self._md_style is not None else pillowmd.MdStyle()
        )

        # 渲染级参数
        render_kwargs = {}
        title = kwargs.get("title") or ""
        if isinstance(title, str) and title.strip():
            render_kwargs["title"] = title.strip()
        if kwargs.get("autoPage"):
            render_kwargs["autoPage"] = True
        if kwargs.get("noDecoration"):
            render_kwargs["noDecoration"] = True

        # 模板样式渲染器(LoadMarkdownStyles 的同步 Render):
        #     style.Render(md, autoPage=True)   → MdRenderResult(imageType=gif/png)
        # 默认样式是 dataclass,复用 pillowmd.MdToImage(异步)即可。
        if (
            self._md_style is not None
            and not dataclasses.is_dataclass(self._md_style)
            and hasattr(self._md_style, "Render")
        ):
            loop = asyncio.get_running_loop()
            img = await loop.run_in_executor(
                None,
                lambda: self._md_style.Render(cleaned, **render_kwargs),
            )
        else:
            img = await pillowmd.MdToImage(cleaned, style=base_style, **render_kwargs)

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self._save_render(img))

    @staticmethod
    def _save_render(img) -> str:
        """把渲染结果(MdRenderResult / PIL Image)存为临时文件并返回路径。

        - 结果声明 `imageType == "gif"` → 存 `.gif`(保存全部帧 + duration + loop=0)。
        - 其余 → 存 `.png`(多图结果取首张)。
        任何失败都会先删除已创建的临时文件再抛出,避免残留空文件。
        """
        image_type = str(getattr(img, "imageType", "") or "").lower()
        is_gif = image_type == "gif"
        suffix = ".gif" if is_gif else ".png"

        fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        try:
            if is_gif:
                return MdToImageMixin._save_as_gif(img, tmp_path)
            return MdToImageMixin._save_as_png(img, tmp_path)
        except Exception:
            _remove_file(tmp_path)
            raise

    @staticmethod
    def _save_as_gif(img, tmp_path: str) -> str:
        """保存 GIF:首帧 + append_images 其余帧 + duration + 无限循环。

        说明:这里用 Pillow 正确的 `duration` 参数(pillowmd 库自己的 `Save`
        错写成了 `duratio` 被 Pillow 忽略,帧时长会退化成默认值)。
        """
        main = getattr(img, "image", None)
        if main is None:
            raise RuntimeError("GIF 渲染结果缺少首帧图片")
        frames = getattr(img, "images", None) or []
        dur_ms = int(float(getattr(img, "gifDuratio", 0.5) or 0.5) * 1000)
        if len(frames) > 1:
            main.save(
                tmp_path,
                save_all=True,
                optimize=True,
                append_images=frames[1:],
                duration=max(10, dur_ms),
                loop=0,
            )
        else:
            main.save(tmp_path)
        return tmp_path

    @staticmethod
    def _save_as_png(img, tmp_path: str) -> str:
        """保存 PNG:优先单张 image,多图结果(multi-page)取首张。"""
        pil_image = getattr(img, "image", None)
        if pil_image is None:
            pages = getattr(img, "images", None) or []
            pil_image = pages[0] if pages else None
        if pil_image is not None and hasattr(pil_image, "save"):
            pil_image.save(tmp_path)
            return tmp_path

        # 兜底:结果自带 Save(directory) / save(path)
        if hasattr(img, "Save"):
            saved = img.Save(Path(tmp_path).parent)
            if saved and os.path.exists(str(saved)):
                os.replace(str(saved), tmp_path)
                return tmp_path
        if hasattr(img, "save"):
            img.save(tmp_path)
            return tmp_path

        raise RuntimeError(f"无法保存图片:未知渲染结果类型 {type(img).__name__}")
