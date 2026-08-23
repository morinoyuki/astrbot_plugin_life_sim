"""图片渲染引擎:MoTalk 风格的剧情转图。

无桌面、无浏览器依赖,纯 Pillow 实现。文本始终转图,支持基础 Markdown。

用法::

    from im_render.engine import render_narrative
    imgs = render_narrative(
        "阿龙:你好呀\\n我:嗨",
        theme="light",
        width=920,
        title="第七章 · 王都篇",
    )
    imgs[0].save("out.png")
"""

from .engine import (
    ChatRenderer,
    Row,
    TooManyPages,
    render_narrative,
)

__all__ = [
    "ChatRenderer",
    "Row",
    "TooManyPages",
    "render_narrative",
]
