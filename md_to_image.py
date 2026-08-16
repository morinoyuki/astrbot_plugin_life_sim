"""Markdown → 图片渲染(基于 pillowmd,无浏览器)。

整合自 astrbot_plugin_nobrowser_markdown_to_pic (Xican) 的核心逻辑,精简为:
- 一个转图开关(output_as_image)
- 模板目录(output_image_style_path,LoadMarkdownStyles 的自定义样式)
- 自动分页开关(output_image_auto_page)

pillowmd 补丁在模块导入时自动应用(幂等、安全)。
"""

import asyncio
import os
import re
import tempfile
import dataclasses

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

    async def md_render_to_path(self, text: str, **kwargs) -> str:
        """渲染 Markdown 为图片,返回临时 PNG 路径。

        kwargs 支持(透传给渲染器):
            title(str)      图片顶部标题
            autoPage(bool)  自动分页
            noDecoration(bool) 透明背景/无装饰
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
        #     style.Render(md, autoPage=True).image
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

        # 保存到临时文件(优先 .image,多图结果取首张,兜底 .Save)
        loop = asyncio.get_running_loop()

        def _save():
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                tmp_path = f.name
            pil_image = getattr(img, "image", None)
            if pil_image is None:
                pages = getattr(img, "images", None) or []
                pil_image = pages[0] if pages else None
            if pil_image is not None and hasattr(pil_image, "save"):
                pil_image.save(tmp_path)
            elif hasattr(img, "Save"):
                img.Save(tmp_path)
            elif hasattr(img, "save"):
                img.save(tmp_path)
            else:
                raise RuntimeError("无法保存图片:未知渲染结果类型")
            return tmp_path

        return await loop.run_in_executor(None, _save)
