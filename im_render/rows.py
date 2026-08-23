"""可复用的行类型。每个 Row 是一个 "宽度已知、高度固定、渲染到 RGBA 图层" 的单元。

VerticalStack 负责把多行垂直拼接到画布;每行内部可以自由布局而不影响其他行,
从而从机制上避免错位 / 重叠。
"""

from __future__ import annotations

from collections.abc import Sequence

from PIL import Image, ImageDraw, ImageFont

from . import markdown as md
from .style import font_for_char, load_font

try:  # noqa: E129
    from .engine import AvatarSource  # noqa: F401
except ImportError:  # pragma: no cover
    pass


def _hex(c):
    c = c.lstrip("#")
    if len(c) == 3:
        c = "".join(x * 2 for x in c)
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))


def _rgba(c, a=255):
    r, g, b = _hex(c)
    return (r, g, b, a)


def font_metrics(font: ImageFont.FreeTypeFont) -> int:
    """返回字体行高(含行距)。"""
    top, bottom = font.getmetrics()
    return top + bottom


def make_canvas(width: int, height: int, rgb: tuple = (0, 0, 0, 0)):
    """创建 RGBA 画布 + 绘制对象。"""
    img = Image.new("RGBA", (max(1, width), max(1, height)), rgb)
    return img, ImageDraw.Draw(img)


class Row:
    """行基类。所有子类必须实现 :meth:`_render_impl`,在 (0,0) 处绘制。"""

    def __init__(self, r: object):
        from .engine import ChatRenderer

        self.r: ChatRenderer = r
        self.height: int = 10

    def measure(self) -> Row:
        """计算高度(子类可覆盖)。返回 self。"""
        return self

    def draw(self, canvas: Image.Image, y: int) -> None:
        """把本行内容绘制到 canvas 的 y 处(左上角)。"""
        # 直接绘制在 y 偏移
        self._paint_on(canvas, y)

    def _paint_on(self, canvas: Image.Image, y: int) -> None:
        raise NotImplementedError

    # 便捷工具
    def _draw_spans(
        self,
        canvas: Image.Image,
        x: int,
        y: int,
        spans: Sequence[md.Span],
        default_color,
        font_size: int | None = None,
    ) -> int:
        """绘制富文本 span 序列,返回结束的 x 坐标。不换行。逐字符支持 emoji 回退。"""
        draw = ImageDraw.Draw(canvas)
        cx = x
        fs = font_size or self.r.font_size
        for sp in spans:
            color = _rgba(self.r.t.link) if sp.link else _rgba(default_color)
            # 逐字符绘制以支持 emoji / 符号字体回退
            for ch in sp.text:
                f = font_for_char(ch, fs, bold=sp.bold)
                draw.text((cx, y), ch, font=f, fill=color)
                cw = draw.textlength(ch, font=f)
                if sp.strike:
                    mid = y + f.getmetrics()[0] // 2
                    draw.line(
                        [(cx, mid), (cx + cw, mid)], fill=color, width=max(1, fs // 20)
                    )
                cx += cw
        return cx

    def _measure_spans(self, spans: Sequence[md.Span], font_size: int) -> float:
        """测量 span 序列的总宽度。"""
        from PIL import ImageDraw

        draw = ImageDraw.Draw(Image.new("RGB", (4, 4)))
        total = 0.0
        for sp in spans:
            f = load_font(font_size, bold=sp.bold)
            total += draw.textlength(sp.text, font=f)
        return total

    def _wrap_spans(
        self,
        spans: Sequence[md.Span],
        max_width: int,
        font_size: int,
    ) -> list[list[md.Span]]:
        """把 spans 按最大宽度换行,返回多行。"""
        if not spans:
            return [[]]
        lines: list[list[md.Span]] = []
        cur: list[md.Span] = []
        cur_w = 0.0
        draw = ImageDraw.Draw(Image.new("RGB", (4, 4)))

        for sp in spans:
            for ch in sp.text:
                f = load_font(font_size, bold=sp.bold)
                cw = draw.textlength(ch, font=f)
                if cur and cur_w + cw > max_width:
                    lines.append(cur)
                    cur = []
                    cur_w = 0.0
                cur.append(
                    md.Span(
                        ch,
                        bold=sp.bold,
                        italic=sp.italic,
                        strike=sp.strike,
                        code=sp.code,
                        link=sp.link,
                    )
                )
                cur_w += cw
        if cur:
            lines.append(cur)
        return lines


class RichTextRow(Row):
    """富文本段落行(单行,由上层保证不超宽)。"""

    def __init__(
        self,
        r,
        spans: Sequence[md.Span],
        *,
        font_size: int | None = None,
        bold: bool = False,
        color: str | None = None,
        align: str = "left",
        margin=(0, 0, 0, 0),
        border_left: int = 0,
        dedent: bool = True,
    ):
        super().__init__(r)
        self.bold = bold
        self.color = color or r.t.text_secondary
        self.font_size = font_size or r.font_size
        self.align = align
        self.margin = margin
        self.border_left = border_left

        # 可用内容宽度(全宽 - 左右 margin - border)
        max_w = r.width - r.h_pad * 2 - margin[2] - margin[3] - (border_left > 0) * 10
        if self.border_left:
            max_w -= 8

        # 先按显式换行符拆成子段(span.text 可能含 \n),再分别换行
        para_lines: list[list[md.Span]] = []
        for sp in spans:
            parts = sp.text.split("\n")
            for i, pt in enumerate(parts):
                if i == 0:
                    if para_lines:
                        para_lines[-1].append(
                            md.Span(
                                pt,
                                bold=sp.bold,
                                italic=sp.italic,
                                strike=sp.strike,
                                code=sp.code,
                                link=sp.link,
                            )
                        )
                    else:
                        para_lines.append(
                            [
                                md.Span(
                                    pt,
                                    bold=sp.bold,
                                    italic=sp.italic,
                                    strike=sp.strike,
                                    code=sp.code,
                                    link=sp.link,
                                )
                            ]
                        )
                else:
                    para_lines.append(
                        [
                            md.Span(
                                pt if pt else " ",
                                bold=sp.bold,
                                italic=sp.italic,
                                strike=sp.strike,
                                code=sp.code,
                                link=sp.link,
                            )
                        ]
                    )

        # 每段按像素换行
        wrapped: list[list[md.Span]] = []
        for pl in para_lines:
            ws = self._wrap_spans(pl, max_w, self.font_size)
            wrapped.extend(ws)
        self.lines = wrapped

        # 计算高度
        font = load_font(self.font_size, bold=bold)
        self.text_h = font_metrics(font)
        line_h = int(self.text_h * r.line_height) if self.text_h else self.font_size
        pad_y = margin[0] + margin[1]
        self.height = max(self.font_size, len(self.lines) * line_h + pad_y)

    def _paint_on(self, canvas: Image.Image, y: int) -> None:
        draw = ImageDraw.Draw(canvas)
        x0 = self.r.h_pad + self.margin[2]
        ty = y + self.margin[0]

        if self.border_left:
            draw.rectangle(
                [self.r.h_pad, y, self.r.h_pad + 4, y + self.height - self.margin[1]],
                fill=_rgba(self.r.t.link),
            )
            x0 += 8

        color = _rgba(self.color)
        line_h = (
            int(self.text_h * self.r.line_height) if self.text_h else self.font_size
        )
        for line in self.lines:
            x = x0
            for sp in line:
                col = _rgba(self.r.t.link) if sp.link else color
                for ch in sp.text:
                    f = font_for_char(ch, self.font_size, bold=sp.bold or self.bold)
                    draw.text((x, ty), ch, font=f, fill=col)
                    x += draw.textlength(ch, font=f)
            ty += line_h


class CodeRow(Row):
    """代码块行。"""

    def __init__(self, r, code: str, lang: str = ""):
        super().__init__(r)
        self.code = code
        self.lang = lang
        self.fs = max(11, int(r.font_size * 0.85))
        self.font = load_font(self.fs)
        self.line_h = font_metrics(self.font) + 4

        lines = code.split("\n") or [""]
        self.lines = lines
        self.pad = 10
        self.height = len(lines) * self.line_h + self.pad * 2

    def _paint_on(self, canvas: Image.Image, y: int) -> None:
        draw = ImageDraw.Draw(canvas)
        x0 = self.r.h_pad
        x1 = self.r.width - self.r.h_pad
        draw.rectangle(
            [x0, y, x1, y + self.height],
            fill=_rgba(self.r.t.code_bg),
        )
        ty = y + self.pad
        for line in self.lines:
            draw.text(
                (x0 + self.pad, ty),
                line,
                font=self.font,
                fill=_rgba(self.r.t.text_secondary),
            )
            ty += self.line_h


class EmptyRow(Row):
    """占位空白行。"""

    def __init__(self, r, height: int):
        super().__init__(r)
        self.height = max(0, int(height))

    def _paint_on(self, canvas: Image.Image, y: int) -> None:
        pass


class PillRow(Row):
    """居中灰色胶囊行(MomoTalk 剧情事件 / 系统提示样式)。

    用于穿插在对话间的短旁白(如“我微微停顿,没有立刻回答。”),
    视觉上与对话气泡区分,避免割裂。
    """

    def __init__(
        self, r, text: str, *, font_size: int | None = None, color: str | None = None
    ):
        super().__init__(r)
        self.text = text.strip()
        self.font_size = font_size or int(r.font_size * 0.92)
        self.color = color or r.t.pill_text

        # 换行
        font = load_font(self.font_size)
        max_w = int(r.width * 0.72)
        sw = font.getlength(self.text)
        if sw > max_w:
            # 手动按字换行
            lines = []
            cur = ""
            d = ImageDraw.Draw(Image.new("RGB", (4, 4)))
            for ch in self.text:
                if cur and d.textlength(cur + ch, font=font) > max_w:
                    lines.append(cur)
                    cur = ch
                else:
                    cur += ch
            if cur:
                lines.append(cur)
            self.lines = lines
        else:
            self.lines = [self.text]

        # 高度:行数 * 行高 + 上下内边距
        line_h = int(font.size * 1.7)
        self.line_h = line_h
        self.pad_v = max(5, int(font.size * 0.35))
        self.height = len(self.lines) * line_h + self.pad_v * 2

    def _paint_on(self, canvas: Image.Image, y: int) -> None:
        draw = ImageDraw.Draw(canvas)
        font = load_font(self.font_size)
        line_h = self.line_h
        pad_x = int(self.font_size * 0.9)
        color = _rgba(self.color)

        # 逐行绘制居中胶囊
        ty = y + self.pad_v
        pill_color = _rgba(self.r.t.pill_bg)
        for line in self.lines:
            tw = font.getlength(line)
            pw = tw + pad_x * 2
            px = (self.r.width - pw) / 2
            draw.rounded_rectangle(
                [px, ty, px + pw, ty + line_h],
                radius=line_h / 2,
                fill=pill_color,
            )
            draw.text(
                (px + pad_x, ty + (line_h - font.size) / 2),
                line,
                font=font,
                fill=color,
            )
            ty += line_h


class HrRow(Row):
    def __init__(self, r):
        super().__init__(r)
        self.height = max(6, r.v_pad // 2)

    def _paint_on(self, canvas: Image.Image, y: int) -> None:
        draw = ImageDraw.Draw(canvas)
        mid = y + self.height // 2
        draw.line(
            [(self.r.h_pad, mid), (self.r.width - self.r.h_pad, mid)],
            fill=_rgba(self.r.t.card_border),
            width=2,
        )


class ImageRow(Row):
    """图片行(支持本地路径 / URL / data URI)。"""

    def __init__(self, r, blk: md.ImageBlock):
        super().__init__(r)
        self.blk = blk
        self._img: Image.Image | None = None
        self._load()

    def _load(self) -> None:
        import io
        import os
        import urllib.request

        url = self.blk.url
        data = None
        try:
            if url.startswith("data:"):
                import base64

                b64 = url.split(",", 1)[1]
                data = base64.b64decode(b64)
            elif url.startswith(("http://", "https://")):
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = resp.read()
            elif os.path.isfile(url):
                with open(url, "rb") as f:
                    data = f.read()
        except Exception:
            data = None

        self._img = None
        if data:
            try:
                self._img = Image.open(io.BytesIO(data)).convert("RGBA")
            except Exception:
                self._img = None

        max_w = self.r.width - self.r.h_pad * 2
        max_h = max(60, self.r.page_max_height // 5)
        if self._img is not None:
            self._img.thumbnail((max_w, max_h), Image.LANCZOS)
            self.height = self._img.height
        else:
            # 占位
            self.height = 48
            self._img = None

    def _paint_on(self, canvas: Image.Image, y: int) -> None:
        if self._img is None:
            draw = ImageDraw.Draw(canvas)
            x0 = self.r.h_pad
            draw.rectangle(
                [x0, y, self.r.width - self.r.h_pad, y + self.height],
                fill=_rgba(self.r.t.code_bg),
            )
            msg = f"🖼️ {self.blk.alt or '(未加载)'}"
            draw.text(
                (x0 + 8, y + (self.height - self.r.font_size) // 2),
                msg,
                font=load_font(int(self.r.font_size * 0.85)),
                fill=_rgba(self.r.t.text_muted),
            )
            return
        x = (self.r.width - self._img.width) // 2
        canvas.alpha_composite(self._img, (x, y))


class DialogueRow(Row):
    """聊天气泡行:momotalk 风格。

    布局(对方为例)::

        ┌────┐ 名字
        │头像│ ┌──────────┐
        │    │ │  气泡     │
        │    │ └──────────┘
        └────┘

    - 头像固定在左(对方)/ 右(自己),顶部与名字第一行对齐
    - 名字在头像右侧、气泡上方(自己无名字)
    - 头像始终保持正方形,尺寸 = avatar_size
    """

    def __init__(
        self,
        r,
        *,
        speaker: str,
        spans: Sequence[md.Span],
        is_self: bool = False,
        avatar=None,
    ):
        super().__init__(r)
        self.speaker = speaker
        self.spans = spans
        self.is_self = is_self
        self.avatar = avatar

        self.fs = r.font_size
        # 间距配置
        self.gap = max(10, int(r.font_size * 0.4))  # 头像与内容间距
        self.margin_lr = 14  # 气泡水平内边距
        self.margin_v = 10  # 气泡垂直内边距
        # 名字行高(即名字占用的高度)
        self.name_h = r.name_font_size + 4
        # 名字与气泡间距
        self.name_bubble_gap = 4
        self.av = r.avatar_size
        self._compute_layout()

    def _content_x(self) -> int:
        """内容(名字/气泡)左边界(对方)。"""
        return self.r.h_pad + self.av + self.gap

    def _compute_layout(self):
        r = self.r
        # 可用内容总宽
        content_w = r.width - r.h_pad * 2
        max_text = int(content_w * r.max_bubble_ratio)
        self.line_h = int(self.fs * r.line_height)

        # 换行
        self.wrapped_lines = self._wrap_spans(self.spans, max_text, self.fs)
        if not self.wrapped_lines:
            self.wrapped_lines = [[md.Span(" ")]]

        # 最大行宽
        max_line_w = self._max_line_width(self.wrapped_lines)

        # 气泡尺寸
        self.bubble_w = int(min(max_line_w, max_text) + self.margin_lr * 2)
        self.bubble_w = max(int(r.font_size * 1.2), self.bubble_w)
        self.bubble_h = len(self.wrapped_lines) * self.line_h + self.margin_v * 2

        # 右侧内容高度 = 名字 + 间距 + 气泡(自己/对方都显示名字,像微信)
        if self.speaker:
            right_h = self.name_h + self.name_bubble_gap + self.bubble_h
        else:
            right_h = self.bubble_h

        # 行高 = max(头像, 右侧内容)
        self.height = int(max(self.av, right_h) + 2)

    def _measure_spans_px(self, spans, font_size):
        from PIL import ImageDraw

        draw = ImageDraw.Draw(Image.new("RGB", (4, 4)))
        total = 0.0
        for sp in spans:
            f = load_font(font_size, bold=sp.bold)
            total += draw.textlength(sp.text, font=f)
        return total

    def _max_line_width(self, lines) -> float:
        max_w = 0.0
        for line in lines:
            max_w = max(max_w, self._measure_spans_px(line, self.fs))
        return max_w

    def _paint_on(self, canvas: Image.Image, y: int) -> None:
        r = self.r
        y0 = y

        # ---- 自己/对方区分布局 ----
        if self.is_self:
            # 头像固定在右侧边缘
            av_x = r.width - r.h_pad - self.av
            # 气泡右边界 = 头像左边界 - gap
            bubble_right = av_x - self.gap
            bubble_x = bubble_right - self.bubble_w
            bubble_x = max(r.h_pad, bubble_x)  # 防越界
        else:
            av_x = r.h_pad
            bubble_x = self._content_x()

        # 名字 / 气泡 的起始 y(顶部对齐)
        outer_top = y0 + 1

        # 画头像(固定左侧或右侧,高度方向顶部对齐)
        self._draw_avatar(canvas, av_x, outer_top, self.av)

        # 气泡位置:名字下方(自己/对方都显示名字)
        name_h = self.name_h + self.name_bubble_gap if self.speaker else 0
        if self.speaker:
            bubble_y = outer_top + name_h
        else:
            bubble_y = outer_top

        # 绘制气泡背景
        bubble_bg = r.t.bubble_self if self.is_self else r.t.bubble_other
        self._draw_bubble(
            canvas,
            bubble_x,
            bubble_y,
            self.bubble_w,
            self.bubble_h,
            _rgba(bubble_bg),
        )

        # 名字(在气泡上方;自己的名字右对齐,与头像同侧)
        if self.speaker:
            draw = ImageDraw.Draw(canvas)
            name_font = load_font(r.name_font_size)
            if not self.is_self:
                name_x = self._content_x()
            else:
                # 自己的名字右对齐到头像左边界前
                avatar_left = r.width - r.h_pad - self.av
                name_x = (
                    avatar_left
                    - self.gap
                    - int(draw.textlength(self.speaker, font=name_font))
                )
                name_x = max(r.h_pad, name_x)
            draw.text(
                (name_x, outer_top),
                self.speaker,
                font=name_font,
                fill=_rgba(r.t.name_color),
            )

        # 文本(在气泡内垂直居中)
        # bubble_h = 行数*line_h + margin_v*2,每个 line_h 槽位内文字应居中
        font0 = load_font(self.fs)
        _asc, _desc = font0.getmetrics()
        font_line_h = _asc + _desc  # 文字实际像素高度
        slot_offset = max(0, (self.line_h - font_line_h) / 2)

        tx = bubble_x + self.margin_lr
        ty = bubble_y + self.margin_v + slot_offset
        bubble_fg = r.t.bubble_self_text if self.is_self else r.t.bubble_other_text
        for line in self.wrapped_lines:
            self._draw_spans_at(canvas, tx, ty, line, bubble_fg)
            ty += self.line_h

    def _draw_spans_at(self, canvas, x, y, spans, color):
        draw = ImageDraw.Draw(canvas)
        cx = x
        for sp in spans:
            col = _rgba(self.r.t.link) if sp.link else _hex(color) + (255,)
            for ch in sp.text:
                f = font_for_char(ch, self.fs, bold=sp.bold)
                draw.text((cx, y), ch, font=f, fill=col)
                cx += draw.textlength(ch, font=f)

    def _draw_bubble(self, canvas, x, y, w, h, fill):
        draw = ImageDraw.Draw(canvas)
        radius = min(self.r.bubble_radius, h // 2)
        draw.rounded_rectangle(
            [x, y, x + w, y + h],
            radius=radius,
            fill=fill,
        )

    def _draw_avatar(self, canvas, x, y, size):
        img: Image.Image | None = None
        src = self.avatar
        if src is not None:
            try:
                if hasattr(src, "convert"):
                    img = src.convert("RGBA")
                else:
                    img = Image.open(src).convert("RGBA")
                img = img.resize((size, size), Image.LANCZOS)
            except Exception:
                img = None

        if img is None:
            # 占位:名字首字符
            img = Image.new("RGBA", (size, size), (203, 213, 224, 255))
            d = ImageDraw.Draw(img)
            ch = (self.speaker or "?")[:1]
            f = load_font(int(size * 0.5))
            bbox = d.textbbox((0, 0), ch, font=f)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            d.text(
                ((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]),
                ch,
                font=f,
                fill=(255, 255, 255, 255),
            )

        # 圆角蒙版
        mask = Image.new("L", (size, size), 0)
        md = ImageDraw.Draw(mask)
        radius = int(size * 0.24)
        md.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
        canvas.paste(img, (x, y), mask)
