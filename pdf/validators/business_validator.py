"""业务数学校验（方案 §8/§23）：逐行 + 合计四类关系。

  · 数量 × 单价 ≈ 金额（逐行）
  · 金额 × 税率 ≈ 税额（逐行）
  · Σ 明细金额 ≈ 合计金额
  · Σ 明细税额 ≈ 合计税额
  · 合计金额 + 合计税额 ≈ 价税合计

为什么必需：双引擎交叉验证只能证明"两个引擎切词一致"——两个引擎跑的是**同一套
锚点规则**，锚点指错地方时两边会一起错、一起"一致"。业务数学是独立信息源。

明细已逐行重建（report.items，Table Engine 输出）时走逐行 + Σ 校验；
未接入表格的模板退回单行/行数统计模式（旧行为不变）。

金额全部用 Decimal + 容差比较（base.MONEY_TOLERANCE = 0.02 元）；超容差再分两级：
尾差 → warning（转人工），明显不成立 → error（判失败）。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from pdf.validators.base import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    CheckResult,
    approx_equal,
    money,
    severity_for,
    to_decimal,
    to_rate,
)


def _value(report: Any, field_name: str | None) -> str | None:
    if not field_name:
        return None
    result = report.fields.get(field_name)
    if result is None or not result.valid:
        return None
    return result.normalized_value


def _int_value(report: Any, field_name: str | None) -> int | None:
    value = _value(report, field_name)
    if value is None or not str(value).strip():
        return None
    try:
        return int(float(str(value).strip()))
    except ValueError:
        return None


def validate_business(template: dict[str, Any], report: Any) -> list[CheckResult]:
    """按模板 business_check 配置执行业务数学校验。

    明细已逐行重建（report.items）时走逐行 + Σ 校验（方案 §23）；
    否则退回单行/行数统计模式（旧模板兼容）。
    """
    config = template.get("business_check") or {}
    if not config:
        return []

    items = list(getattr(report, "items", None) or [])
    if items:
        return _validate_items_math(config, report, items)

    results: list[CheckResult] = []
    item = config.get("item") or {}
    totals = config.get("totals") or {}
    rows = _int_value(report, config.get("rows_field", "item_rows"))

    results.extend(_check_item_math(item, report, rows))
    results.extend(_check_grand_total(totals, report))
    return results


# ---------------------------------------------------------------- 逐行 + Σ（items 模式）


def _validate_items_math(
    config: dict[str, Any], report: Any, items: list[dict[str, Any]]
) -> list[CheckResult]:
    """items 模式：逐行两条关系 + Σ明细≈合计两条 + 合计金额+税额≈价税合计。"""
    results = _check_rows_math(items)
    results.extend(_check_sums(config, report, items))
    results.extend(_check_grand_total(config.get("totals") or {}, report))
    return results


def _check_rows_math(items: list[dict[str, Any]]) -> list[CheckResult]:
    """逐行：数量×单价≈金额、金额×税率≈税额；行内缺值只跳过该行（不误报）。"""
    amount_checks: list[tuple[bool, str, str]] = []
    tax_checks: list[tuple[bool, str, str]] = []
    for item in items:
        row = item.get("row_index")
        quantity = to_decimal(item.get("quantity"))
        unit_price = to_decimal(item.get("unit_price"))
        amount = to_decimal(item.get("amount"))
        if quantity is not None and unit_price is not None and amount is not None:
            expected = quantity * unit_price
            amount_checks.append(
                (
                    approx_equal(expected, amount),
                    f"第{row}行 数量×单价={money(expected)} vs 金额={money(amount)}",
                    severity_for(expected, amount),
                )
            )
        rate = to_rate(item.get("tax_rate"))
        tax = to_decimal(item.get("tax"))
        if rate is not None and amount is not None and tax is not None:
            expected = (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            tax_checks.append(
                (
                    approx_equal(expected, tax),
                    f"第{row}行 金额×税率={money(expected)} vs 税额={money(tax)}",
                    severity_for(expected, tax),
                )
            )
    return [
        _aggregate("item_amount", amount_checks, "数量/单价/金额不完整，跳过逐行校验"),
        _aggregate("item_tax", tax_checks, "金额/税率/税额不完整（免税、不征税等），跳过逐行校验"),
    ]


def _aggregate(rule: str, checks: list[tuple[bool, str, str]], skip_detail: str) -> CheckResult:
    """把逐行校验结果聚合为一条结论（全部通过才算通过）。"""
    if not checks:
        return CheckResult(rule, True, skip_detail, skipped=True)
    failures = [(detail, severity) for passed, detail, severity in checks if not passed]
    if not failures:
        return CheckResult(rule, True, f"{len(checks)} 行全部通过")
    severity = SEVERITY_ERROR if any(s == SEVERITY_ERROR for _, s in failures) else SEVERITY_WARNING
    detail = f"{len(failures)}/{len(checks)} 行不成立：" + "；".join(detail for detail, _ in failures[:3])
    return CheckResult(rule, False, detail, severity=severity)


def _check_sums(
    config: dict[str, Any], report: Any, items: list[dict[str, Any]]
) -> list[CheckResult]:
    """Σ明细金额≈合计金额、Σ明细税额≈合计税额（方案 §23）。"""
    totals = config.get("totals") or {}
    results: list[CheckResult] = []
    for rule, item_key, field_key, label in (
        ("sum_amount", "amount", totals.get("amount"), "Σ明细金额"),
        ("sum_tax", "tax", totals.get("tax"), "Σ明细税额"),
    ):
        if not field_key:
            continue
        values = [to_decimal(item.get(item_key)) for item in items]
        actual = to_decimal(_value(report, field_key))
        if actual is None or any(value is None for value in values):
            results.append(
                CheckResult(
                    rule,
                    True,
                    f"{label} 与合计行数据不完整，跳过累加校验",
                    skipped=True,
                    fields=(field_key,),
                )
            )
            continue
        expected = sum(values, Decimal("0"))
        value_details = " + ".join(money(value) for value in values[:5])
        if len(values) > 5:
            value_details += f" + …（共 {len(values)} 行）"
        results.append(
            CheckResult(
                rule,
                approx_equal(expected, actual),
                f"{label}={money(expected)}（明细：{value_details}） vs 合计={money(actual)}",
                severity=severity_for(expected, actual),
                fields=(field_key,),
            )
        )
    return results


def _check_item_math(item: dict[str, Any], report: Any, rows: int | None) -> list[CheckResult]:
    if not item:
        return []
    qty_field = item.get("quantity")
    price_field = item.get("unit_price")
    amount_field = item.get("amount")
    rate_field = item.get("tax_rate")
    tax_field = item.get("tax")

    if rows is None:
        return [
            CheckResult(
                "item_amount",
                True,
                "明细行数未知，跳过明细级数学校验",
                skipped=True,
                fields=tuple(f for f in (qty_field, price_field, amount_field) if f),
            )
        ]
    if rows > 1:
        return [
            CheckResult(
                "item_amount",
                True,
                f"明细 {rows} 行未逐行提取，跳过明细级数学校验（Σ明细需逐行数据）",
                skipped=True,
                fields=tuple(f for f in (qty_field, price_field, amount_field) if f),
            ),
            CheckResult(
                "sum_amount",
                True,
                f"明细 {rows} 行未逐行提取，Σ明细金额 ≈ 合计金额 暂不可校验",
                skipped=True,
                fields=(amount_field,) if amount_field else (),
            ),
            CheckResult(
                "sum_tax",
                True,
                f"明细 {rows} 行未逐行提取，Σ明细税额 ≈ 合计税额 暂不可校验",
                skipped=True,
                fields=(tax_field,) if tax_field else (),
            ),
        ]

    results: list[CheckResult] = []
    quantity = to_decimal(_value(report, qty_field))
    unit_price = to_decimal(_value(report, price_field))
    amount = to_decimal(_value(report, amount_field))

    if quantity is not None and unit_price is not None and amount is not None:
        expected = quantity * unit_price
        results.append(
            CheckResult(
                "item_amount",
                approx_equal(expected, amount),
                f"数量×单价={money(expected)} vs 金额={money(amount)}",
                severity=severity_for(expected, amount),
                fields=tuple(f for f in (qty_field, price_field, amount_field) if f),
            )
        )
    else:
        results.append(
            CheckResult(
                "item_amount",
                True,
                "数量/单价/金额有缺失，跳过 数量×单价≈金额",
                skipped=True,
                fields=tuple(f for f in (qty_field, price_field, amount_field) if f),
            )
        )

    rate = to_rate(_value(report, rate_field))
    tax = to_decimal(_value(report, tax_field))
    if rate is not None and amount is not None and tax is not None:
        expected = (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        results.append(
            CheckResult(
                "item_tax",
                approx_equal(expected, tax),
                f"金额×税率={money(expected)} vs 税额={money(tax)}",
                severity=severity_for(expected, tax),
                fields=tuple(f for f in (amount_field, rate_field, tax_field) if f),
            )
        )
    else:
        results.append(
            CheckResult(
                "item_tax",
                True,
                "金额/税率/税额有缺失（免税、不征税等）或税率不可解析，跳过 金额×税率≈税额",
                skipped=True,
                fields=tuple(f for f in (amount_field, rate_field, tax_field) if f),
            )
        )
    return results


def _check_grand_total(totals: dict[str, Any], report: Any) -> list[CheckResult]:
    amount_field = totals.get("amount")
    tax_field = totals.get("tax")
    grand_field = totals.get("grand")
    if not (amount_field and tax_field and grand_field):
        return []

    amount = to_decimal(_value(report, amount_field))
    tax = to_decimal(_value(report, tax_field))
    grand = to_decimal(_value(report, grand_field))
    fields = (amount_field, tax_field, grand_field)

    if amount is None or tax is None or grand is None:
        return [
            CheckResult(
                "grand_total",
                True,
                "金额合计/税额合计/价税合计有缺失，跳过 金额合计+税额合计≈价税合计",
                skipped=True,
                fields=fields,
            )
        ]

    expected = amount + tax
    return [
        CheckResult(
            "grand_total",
            approx_equal(expected, grand),
            f"金额合计+税额合计={money(expected)} vs 价税合计={money(grand)}",
            severity=severity_for(expected, grand),
            fields=fields,
        )
    ]
