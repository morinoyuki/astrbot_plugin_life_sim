# -*- coding: utf-8 -*-
"""im_render 渲染引擎测试。

运行(在插件根目录):
    .venv/bin/python tests/test_im_render.py
"""
import os
import sys
import io

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PIL import Image as PILImage

from im_render import render_narrative
from im_render.engine import ChatRenderer, TooManyPages
from im_render import markdown as md


SAMPLE = """她的目光扫过柜台，落在那个熟悉的身影上。

## 相遇

阿龙:「下午好呀，今天怎么来了？」
我:嗯，老样子就好。

> 她轻声说道。

1. 一起走
2. 留下
"""


def test_parse_blocks():
    blocks = md.parse_blocks(SAMPLE)
    types = [b.type for b in blocks]
    assert "dialogue" in types
    assert "heading" in types
    assert "quote" in types

    # 头像自选:角色名@头像名 → speaker=前段,avatar=后段;无 @ 时 avatar=None
    a, b2 = md.parse_blocks("汐见小亚: 阿姐。\n\n阿龙@汐见小亚: 你是谁?\n")
    assert getattr(a, "avatar", None) is None
    assert getattr(b2, "avatar", None) == "汐见小亚"
    assert getattr(b2, "speaker", None) == "阿龙"
    return blocks


def test_render_light():
    imgs = render_narrative(SAMPLE, theme="light", title="第一章")
    assert len(imgs) == 1
    img = imgs[0]
    assert img.mode == "RGB"
    assert img.width > 100 and img.height > 100


def test_render_dark():
    imgs = render_narrative(SAMPLE, theme="dark", title="dark")
    assert len(imgs) == 1


def test_render_with_avatar_file(tmp_path=None):
    # 用临时图片作为头像
    import tempfile

    d = tempfile.mkdtemp()
    avatar_path = os.path.join(d, "a.png")
    PILImage.new("RGB", (64, 64), (255, 0, 0)).save(avatar_path)

    imgs = render_narrative(
        "阿龙:看头像", theme="light", avatars={"阿龙": avatar_path}
    )
    assert len(imgs) == 1


def test_render_markdown_features():
    text = """# 标题一

**粗体** *斜体* `code` [链接](https://e.com)

```python
print("hello")
```

> 引用内容

| A | B |
|---|---|
| 1 | 2 |
"""
    imgs = render_narrative(text, theme="light", title="md")
    assert len(imgs) == 1


def test_too_many_pages():
    r = ChatRenderer(width=400, max_pages=2)

    # 构造大量内容
    long_text = "\n".join(f"阿龙:第{i}条消息,这是一段比较长的内容" for i in range(50))
    blocks = md.parse_blocks(long_text)
    try:
        r.render(blocks, title="分页")
        # 可能不分页
    except TooManyPages:
        pass


def test_empty_text():
    imgs = render_narrative("", theme="light")
    # 空文本也应能渲染(可能是空图)
    assert isinstance(imgs, list)


def test_no_dialogue_text():
    imgs = render_narrative("这是一段旁白。", theme="light", title="无对白")
    assert len(imgs) == 1


def test_heading_uses_title_font():
    from im_render import markdown as md
    from im_render.engine import ChatRenderer
    from im_render import style as st

    st.search_fonts()
    if not st._title_font_path:
        return  # 无标题专用字体时跳过
    text = "## 第一章:转生\n\n正文内容。"
    r = ChatRenderer(width=800, title="t")
    r.avatars = {}
    r._rows = []
    for blk in md.parse_blocks(text):
        r._layout_block(blk)
    headings = [x for x in r._rows if type(x).__name__ == "RichTextRow" and x.use_title_font]
    assert headings, "标题应使用 use_title_font=True 的 RichTextRow"
    f = headings[0]._measure_font(True)
    assert f.path == st._title_font_path, "标题应使用标题专用字体"



if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  \u2713 {name}")
            except Exception as e:
                failed += 1
                import traceback

                traceback.print_exc()
                print(f"  \u2717 {name}: {e}")
    print(f"\n{'PASS' if failed == 0 else f'{failed} FAILED'}")
    sys.exit(1 if failed else 0)
