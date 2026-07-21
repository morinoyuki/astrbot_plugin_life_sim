"""骰子工具 — 模式 C(DND 跑团)专用"""

import random
import re
from astrbot.api.event import AstrMessageEvent


def _roll_dice_expr(expr: str) -> tuple:
    """掷骰子,返回 (total, detail_string)。

    支持格式:
      NdM         — 投 N 个 M 面骰,如 d20 / 2d6
      NdM+X       — 加常量修正,如 2d6+3 / d20+5
      NdM-X       — 减常量修正
      NdMk{h,l}X  — 投 N 骰,保留最高/最低 X 个
                    如 4d6kh3(DND 属性)、2d20kh1(优势)、2d20kl1(劣势)

    参数范围:骰子数 1~100,面数 2~1000。
    """
    expr = expr.strip().lower().replace(" ", "")
    if not expr:
        raise ValueError("空表达式")

    total = 0
    terms = []
    pos = 0
    first = True

    while pos < len(expr):
        sign = 1
        if expr[pos] == "+":
            pos += 1
        elif expr[pos] == "-":
            sign = -1
            pos += 1
        start = pos
        while pos < len(expr) and expr[pos] not in "+-":
            pos += 1
        term = expr[start:pos]
        if not term:
            if pos >= len(expr):
                break
            continue

        m = re.match(r"^(\d*)d(\d+)(?:k([hl])(\d+))?$", term)
        if m:
            n = int(m.group(1) or 1)
            sides = int(m.group(2))
            keep = m.group(3)
            keep_n = int(m.group(4)) if m.group(4) else None

            if n < 1 or n > 100:
                raise ValueError(f"骰子数超出范围(1~100): {n}")
            if sides < 2 or sides > 1000:
                raise ValueError(f"骰子面数超出范围(2~1000): {sides}")

            rolls = [random.randint(1, sides) for _ in range(n)]
            kept = list(rolls)

            if keep == "h" and keep_n and 0 < keep_n < n:
                sorted_r = sorted(rolls, reverse=True)
                kept = sorted_r[:keep_n]
                dropped = sorted_r[keep_n:]
                rolls_detail = f"[{','.join(map(str, kept))}](kh{keep_n} 弃[{','.join(map(str, dropped))}])"
                sub = sum(kept)
            elif keep == "l" and keep_n and 0 < keep_n < n:
                sorted_r = sorted(rolls)
                kept = sorted_r[:keep_n]
                dropped = sorted_r[keep_n:]
                rolls_detail = f"[{','.join(map(str, kept))}](kl{keep_n} 弃[{','.join(map(str, dropped))}])"
                sub = sum(kept)
            else:
                rolls_detail = f"[{','.join(map(str, rolls))}]"
                sub = sum(rolls)

            dice_label = f"{n}d{sides}"
            if keep:
                dice_label += f"k{keep}{keep_n or ''}"
            prefix = "" if first else ("-" if sign < 0 else "+")
            terms.append(f"{prefix}{dice_label}{rolls_detail}")
            total += sub * sign
            first = False
        elif term.isdigit():
            num = int(term)
            prefix = "" if first else ("-" if sign < 0 else "+")
            terms.append(f"{prefix}{num}")
            total += num * sign
            first = False
        else:
            raise ValueError(f"无法解析: {term}")
    return total, " ".join(terms) if terms else "0"


class DiceMixin:
    """骰子工具 mixin。"""

    async def roll_dice(
        self, event: AstrMessageEvent, expression: str, label: str = ""
    ) -> str:
        """
        Roll dice using standard dice notation. Supports all DND-style rolls.

        Args:
            expression(string): Standard dice notation. Examples:
                - "d20" - one 20-sided die
                - "2d6+3" - two 6-sided dice plus 3
                - "d100" - one 100-sided die (percentile)
                - "4d6kh3" - roll 4d6, keep highest 3 (DND ability score)
                - "2d20kh1" - roll 2d20, keep highest (advantage)
                - "2d20kl1" - roll 2d20, keep lowest (disadvantage)
                - "d20+5" - d20 with +5 modifier (attack roll)
            label(string): Optional. Short description of what this roll is for, like "命中检定 DC15" or "伤害". Can include DC for automatic success/fail comparison.
        Returns:
            Formatted roll result with individual dice, kept dice, total, modifier breakdown, and DC success/fail.
        """
        try:
            total, detail = _roll_dice_expr(expression)
        except ValueError as e:
            return (
                f"❌ 骰子表达式无效:{e}\n"
                f"支持格式:d20 / 2d6+3 / 4d6kh3 / 2d20kh1 / d100 / d20+5"
            )

        # 检定自动判 DC(label 里写 "DC15" / "DC 15" 都识别)
        dc_match = re.search(r"dc\s*(\d+)", (label or "").lower())
        dc_info = ""
        if dc_match:
            dc = int(dc_match.group(1))
            outcome = "✅ 成功" if total >= dc else "❌ 失败"
            dc_info = f"\n🎯 检定结果:{outcome}({total} vs DC{dc})"

        if label:
            return f"🎲 {label}\n{expression} = {detail}\n💥 结果: {total}{dc_info}"
        return f"🎲 {expression} = {detail}\n💥 结果: {total}{dc_info}"
