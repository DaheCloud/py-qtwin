"""字段规则验证：格式校验（正则/类型）+ 业务规则校验。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from pdf.normalizers import normalize_amount, normalize_date


@dataclass
class FieldResult:
    """单个字段的解析结果。"""

    field_name: str
    raw_value: str
    normalized_value: str | None = None
    valid: bool = True
    parser: str = "pymupdf"
    errors: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.valid = False
        self.errors.append(message)


def _check_string(value: str, spec: dict[str, Any]) -> str | None:
    pattern = spec.get("pattern")
    if pattern and not re.fullmatch(pattern, value):
        return f"不匹配正则 {pattern}"
    return None


def _check_decimal(value: str, spec: dict[str, Any]) -> str | None:
    amount = normalize_amount(value)
    if amount is None:
        return "无法解析为金额"
    return None


def _check_date(value: str, spec: dict[str, Any]) -> str | None:
    if normalize_date(value) is None:
        return "无法解析为日期"
    return None


_FORMAT_CHECKERS: dict[str, Callable[[str, dict[str, Any]], str | None]] = {
    "string": _check_string,
    "decimal": _check_decimal,
    "date": _check_date,
}


def validate_field(result: FieldResult, spec: dict[str, Any]) -> FieldResult:
    """按模板字段的 type/pattern 校验单个字段。"""
    if not result.normalized_value:
        result.fail("提取结果为空")
        return result

    ftype = spec.get("type", "string")
    checker = _FORMAT_CHECKERS.get(ftype)
    if checker:
        error = checker(result.normalized_value, spec)
        if error:
            result.fail(error)
    return result


def validate_business_rules(
    fields: dict[str, FieldResult],
    rules: list[dict[str, Any]] | None = None,
) -> list[str]:
    """业务规则校验，返回错误列表。

    内置规则示例：
      {"field": "amount", "op": ">=", "value": "0"}
      {"fields": ["start_date", "end_date"], "op": "ordered"}
      {"field": "customer_name", "op": "not_empty"}
    """
    errors: list[str] = []
    for rule in rules or []:
        op = rule.get("op")

        if op == "not_empty":
            f = fields.get(rule["field"])
            if f is None or not (f.normalized_value or "").strip():
                errors.append(f"{rule['field']} 不能为空")

        elif op in (">=", "<=", ">", "<"):
            f = fields.get(rule["field"])
            if f is None or f.normalized_value is None:
                errors.append(f"{rule['field']} 缺失，无法比较 {op}")
                continue
            amount = normalize_amount(f.normalized_value)
            threshold = normalize_amount(str(rule["value"]))
            if amount is None or threshold is None:
                errors.append(f"{rule['field']} 无法转为数值")
            elif not _compare(amount, op, threshold):
                errors.append(f"{rule['field']}={amount} 不满足 {op} {threshold}")

        elif op == "ordered":
            a, b = rule["fields"]
            da, db = fields.get(a), fields.get(b)
            va = normalize_date(da.normalized_value) if da else None
            vb = normalize_date(db.normalized_value) if db else None
            if va is None or vb is None:
                errors.append(f"{a}/{b} 日期无法解析，无法校验先后顺序")
            elif va > vb:
                errors.append(f"{a}({va}) 晚于 {b}({vb})")

    return errors


def _compare(a: Decimal, op: str, b: Decimal) -> bool:
    return {
        ">=": a >= b,
        "<=": a <= b,
        ">": a > b,
        "<": a < b,
    }[op]
