"""字段标准化：在交叉验证比较之前，统一两个解析器的输出格式。"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_FULLWIDTH_MAP = str.maketrans("０１２３４５６７８９：（）", "0123456789:()")


def normalize_text(value: str | None) -> str:
    """统一文本：去除换行、空格（含全角空格），全角数字转半角。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    text = value.replace("\n", "").replace("\r", "")
    text = text.replace(" ", "").replace("\u3000", "")
    return text.translate(_FULLWIDTH_MAP).strip()


def normalize_amount(value: str | Decimal | None) -> Decimal | None:
    """金额标准化：去掉货币符号、千分位逗号，转 Decimal。

    12,800.00 / ￥12,800 / 12800.00 -> Decimal("12800.00")
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    text = (
        value.replace("￥", "")
        .replace("¥", "")
        .replace(",", "")
        .replace("元", "")
        .strip()
    )
    text = re.sub(r"^[+-]?", lambda m: m.group(0), text)
    text = text.translate(_FULLWIDTH_MAP)
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def normalize_date(value: str | None) -> str | None:
    """日期标准化：2026年9月9日 / 2026/09/09 / 2026-09-09 -> 2026-09-09。"""
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    text = normalize_text(value)
    m = re.search(r"(\d{4})[年/\-.](\d{1,2})[月/\-.](\d{1,2})日?", text)
    if not m:
        return None
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
