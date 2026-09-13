"""校验器公共类型与金额比较工具（方案 §8）。

金额一律用 Decimal 比较，禁止 float——四舍五入/税额尾差都应落在
MONEY_TOLERANCE（分）以内，超出容差再按"尾差"与"明显不成立"分级。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

# 金额比较容差（元）：0.01 元的分位尾差 + 1 分钱的四舍五入余地
MONEY_TOLERANCE = Decimal("0.02")

# "明显不成立"（而非尾差）的判定：绝对差 > max(1 元, 期望值的 1%)
GROSS_ABS = Decimal("1.00")
GROSS_RATIO = Decimal("0.01")

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"


@dataclass(frozen=True)
class CheckResult:
    """一条校验结论。

    passed=False 且 skipped=False 表示校验不成立；skipped=True 表示条件不足未执行
    （例如明细未逐行提取，Σ明细校验无从谈起）——不参与状态判定，但要留痕。
    """

    rule: str
    passed: bool
    detail: str = ""
    skipped: bool = False
    severity: str = SEVERITY_WARNING
    fields: tuple[str, ...] = ()

    @property
    def failed(self) -> bool:
        return not self.passed and not self.skipped

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "rule": self.rule,
            "passed": self.passed,
            "severity": self.severity,
        }
        if self.skipped:
            data["skipped"] = True
        if self.detail:
            data["detail"] = self.detail
        if self.fields:
            data["fields"] = list(self.fields)
        return data


def to_decimal(value: Any) -> Decimal | None:
    """字段值/字符串 → Decimal（含 ¥、千分位清洗）；不可解析返回 None。"""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    text = (
        str(value)
        .strip()
        .replace(",", "")
        .replace("¥", "")
        .replace("￥", "")
    )
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def to_rate(value: Any) -> Decimal | None:
    """税率字段 → 小数比例："9%" → 0.09；"0.09" → 0.09；"免税"/"不征税" → None。

    返回 None 表示"无法比较"（免税、不征税等），调用方应跳过校验而不是判失败。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    rate = to_decimal(text.replace("%", ""))
    if rate is None:
        return None
    if "%" in text or rate > 1:
        return rate / Decimal(100)
    return rate


def approx_equal(a: Decimal | None, b: Decimal | None, tolerance: Decimal = MONEY_TOLERANCE) -> bool:
    """容差比较；任一为 None 视为"无法比较"→ True（不误报）。"""
    if a is None or b is None:
        return True
    return abs(Decimal(a) - Decimal(b)) <= tolerance


def severity_for(expected: Decimal | None, actual: Decimal | None) -> str:
    """超容差时分级：尾差 → warning（转人工），明显不成立 → error（判失败）。"""
    if expected is None or actual is None:
        return SEVERITY_WARNING
    diff = abs(Decimal(expected) - Decimal(actual))
    limit = max(GROSS_ABS, abs(Decimal(expected)) * GROSS_RATIO)
    return SEVERITY_ERROR if diff > limit else SEVERITY_WARNING


def money(value: Decimal) -> str:
    """格式化金额用于提示文案（保留两位小数）。"""
    return format(value.quantize(Decimal("0.01")), "f")
