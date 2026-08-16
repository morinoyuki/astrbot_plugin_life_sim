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
        self._md_style_path = (
            self._cfg("output_image_style_path", "") or ""
        ).strip()

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
        if self._md_style is not None and not dataclasses.is_dataclass(
            self._md_style
        ) and hasattr(self._md_style, "Render"):
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

    # ─── LLM 工具(模型主动渲染富文本) ─────────────────────────

    async def render_markdown_to_image(
        self,
        event,
        markdown: str = "",
        title: str = "",
        auto_page: bool = False,
        transparent_bg: bool = False,
    ) -> str:
        """将 Markdown 文本渲染为图片并发送给用户。当回复包含表格、代码块、标题、列表、公式、引用等富文本排版,文本形式难以清晰展示时调用本工具。

        Args:
            markdown(string): 要渲染的完整 Markdown 文本,支持标题、列表、表格、代码块、公式等语法
            title(string): 可选,图片顶部的标题文字,留空则不显示标题
            auto_page(boolean): 可选,是否自动分页排版(内容很长时可设为 true)
            transparent_bg(boolean): 可选,是否使用透明背景、去除装饰,默认 false
        Returns:
            确认消息。
        """
        md = (markdown or "").strip()
        if not md:
            return "❌ markdown 内容不能为空。"
        try:
            path = await self.md_render_to_path(
                md,
                title=title,
                # 配置开关 output_image_auto_page 作为全局默认,模型显式传 true 也会开启
                autoPage=auto_page or bool(self._cfg("output_image_auto_page", True)),
                noDecoration=transparent_bg,
            )
            event.track_temporary_local_file(path)
            await event.send(event.image_result(path))
            return "✅ Markdown 已渲染为图片并发送给用户。"
        except Exception as e:
            logger.error(f"life-sim: render_markdown_to_image 失败: {e}")
            return f"❌ 渲染失败: {e}"