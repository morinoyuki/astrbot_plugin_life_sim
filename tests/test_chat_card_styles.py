"""聊天卡片新增样式测试:行动选项卡(有序列表→ChoiceRow)、状态胶囊(<t>→TagRow)。

运行(在插件根目录):
    .venv/bin/python tests/test_chat_card_styles.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from im_render import markdown as md
from im_render.engine import ChatRenderer
from im_render.rows import ChoiceRow, TagRow, _measure_fallback


def test_ordered_list_parsed():
    blocks = md.parse_blocks("1. 冲上去救她 — 可能受伤\n2. 逃跑 — 稳妥\n3. 用嘴")
    lst = [b for b in blocks if b.type == "list" and b.ordered]
    assert lst, "有序列表应解析为 ordered list 块"
    assert len(lst[0].items) == 3


def test_tags_block_parsed():
    blocks = md.parse_blocks("<t>HP 78/100</t><t>⬆ 攻击+2</t><t>🔥 灼烧</t>")
    tags = [b for b in blocks if b.type == "tags"]
    assert tags, "标签行应解析为 tags 块"
    items = [md.plain_text(i) for i in tags[0].items]
    assert items == ["HP 78/100", "⬆ 攻击+2", "🔥 灼烧"], items


def test_layout_emits_choice_and_tag_rows():
    r = ChatRenderer(width=800, font_size=30, theme="light")
    # 手构块直接走 _layout_block
    r._rows = []
    r._layout_block(md.ListBlock(True))  # empty
    # 手动构造带 items 的块不便,直接调 parse→render 检查 output
    import io


    text = (
        "# 抉择\n\n"
        "<c>一阵冷风。</c>\n\n"
        "1. 冲上去 — 有风险\n"
        "2. 撤退 — 求稳\n\n"
        "<d name=\"阿龙\" me>听你的。</d>\n\n"
        "<t>HP 90/100</t><t>🔥 灼烧</t>\n"
    )
    imgs = r.render(md.parse_blocks(text))
    assert imgs, "应渲染出图片"
    buf = io.BytesIO()
    imgs[0].save(buf, format="PNG")
    assert buf.getvalue() and len(buf.getvalue()) > 1000


def test_tag_columns_not_splitting():
    r = ChatRenderer(width=800, font_size=30, theme="light")
    row = TagRow(r, ["短", "很短", "更短"])
    assert row.height > 0
    ch = ChoiceRow(r, 1, "一句话的选项", "低风险")
    assert ch.height > 0
    assert ch.label == "一句话的选项"
    assert ch.hint == "低风险"


def test_tag_emoji_width_not_overflow():
    """<t> 胶囊含 emoji 时,测量宽度必须不小于绘制实际占用宽度(否则文字溢出胶囊)。"""
    from PIL import Image, ImageDraw

    from im_render.rows import _measure_fallback

    r = ChatRenderer(width=800, font_size=30, theme="light")
    row = TagRow(r, ["🔥 灼烧", "⬆ 攻击+2", "🛡 防御+3"])
    fs = row.fs
    draw = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    # 每个标签的胶囊宽度 = 文本测量宽 + 两侧 pad_h*2
    for i, line in enumerate(row.lines):
        for t, w in line:
            emoji_measured = _measure_fallback(draw, t, fs)
            # 胶囊宽 w 必须 >= 文本 emoji 感知宽度 + 内边距,保证文字不会超出圆角矩形
            assert w >= emoji_measured + row.pad_h * 2 - 1, \
                f"标签 {t!r} 胶囊过窄: w={w} 文本宽={emoji_measured}"
            # 且不能比绘制端超出(旧 bug 是 w 用 raw getlength 比 emoji_measured 小一个字符宽)
            assert w >= emoji_measured, f"标签 {t!r} 文字将溢出胶囊"


def test_choice_truncation_not_overflow():
    """行动选项卡文字过长截断时,label 右端(含省略号)必须落在卡片右边界内。

    旧 bug:截断测量不含省略号,且 avail 基准比实际绘制起点差约 4px,
    叠加省略号会把「…」挤出卡片右边界。
    """
    from PIL import Image, ImageDraw

    r = ChatRenderer(width=800, font_size=30, theme="light")
    fs = 30
    # 超长选项,含 hint
    long_label = (
        "一个非常非常非常长的行动选项,内容多到必须截断并加省略号,"
        "用来验证省略号不会越出卡片右边界而且文字不能溢出到界外"
    )
    row = ChoiceRow(r, 1, long_label, "后果很大")
    assert row.label.endswith("…"), f"超长 label 应被截断加省略号: {row.label!r}"

    draw = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    # 与 _paint_on 一致的可容纳右端:
    #   有 hint: label_right = width - h_pad - pad_h - hint宽 - 14
    hw = int(_measure_fallback(draw, row.hint, int(fs * 0.72)))
    label_right = r.width - r.h_pad - row.pad_h - hw - 14
    tx = r.h_pad + row.pad_h + 4 + row.badge_d + 14
    label_end = tx + _measure_fallback(draw, row.label, fs)
    assert (
        label_end <= label_right
    ), f"截断后 label 越界: 右端 {label_end:.1f} > 可容纳 {label_right}"

    # 无 hint 情况
    row2 = ChoiceRow(r, 2, long_label + "再来一点更长的文字", "")
    assert row2.label.endswith("…")
    label_right2 = r.width - r.h_pad - row2.pad_h
    tx2 = r.h_pad + row2.pad_h + 4 + row2.badge_d + 14
    label_end2 = tx2 + _measure_fallback(draw, row2.label, fs)
    assert (
        label_end2 <= label_right2
    ), f"无 hint 仍越界: 右端 {label_end2:.1f} > {label_right2}"

    # 短 label 不误加省略号
    row3 = ChoiceRow(r, 3, "短选项", "低风险")
    assert row3.label == "短选项"


if __name__ == "__main__":
    test_ordered_list_parsed()
    print("✓ 有序列表解析为 ordered list")
    test_tags_block_parsed()
    print("✓ <t> 标签行解析为 tags 块")
    test_layout_emits_choice_and_tag_rows()
    print("✓ 渲染输出包含 Choices + Tags 的图片")
    test_tag_columns_not_splitting()
    print("✓ ChoiceRow/TagRow 构造与 hint 拆分")
    test_tag_emoji_width_not_overflow()
    print("✓ <t> 胶囊含 emoji 宽度不溢出")
    test_choice_truncation_not_overflow()
    print("✓ 行动选项卡超长截断不越界(省略号在界内)")
    print("\nPASS")
