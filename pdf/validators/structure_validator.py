"""结构校验：表格重建结果 / 明细表行数 / 各列行数 / 首行字段是否齐全（方案 §7、§22）。

两种模式：
  1. **表格模式**（模板配置了 table 且有重建结果）：直接校验表格结构是否自洽——
     表头是否找到、必需列是否齐全、每条明细的关键单元格是否完整。结论用
     "table_structure_inconsistent" 语义（结构异常 → 转人工复核），不再使用
     "item_rows > 1 → manual_review" 这种粗判（方案 §22）。
  2. **锚点模式**（无表格配置 / 表格未接入）：沿用行数统计 + 各列行数 + 首行
     字段的旧校验，保证既有模板行为不变。

结论只区分"结构是否自洽"：任一检查不成立 → 转人工复核（不判 failed，
因为多数情况是提取不全而非数据本身错误）。
"""

from __future__ import annotations

from typing import Any

from pdf.validators.base import SEVERITY_ERROR, SEVERITY_WARNING, CheckResult


def _raw_value(report: Any, field_name: str) -> str | None:
    if not field_name:
        return None
    result = report.fields.get(field_name)
    if result is None or not result.valid:
        return None
    return result.normalized_value


def _int_value(report: Any, field_name: str) -> int | None:
    """字段值 → 整数（行数类字段）；不可解析返回 None。"""
    value = _raw_value(report, field_name)
    if value is None or not str(value).strip():
        return None
    try:
        return int(float(str(value).strip()))
    except ValueError:
        return None


def validate_structure(template: dict[str, Any], report: Any) -> list[CheckResult]:
    """按模板结构配置校验明细表（表格模式优先，锚点模式回退）。"""
    config = (template.get("structure") or {}).get("item_table")
    if not config:
        return []

    table = getattr(report, "table", None)
    items = list(getattr(report, "items", None) or [])
    if table is not None:
        return _validate_table_structure(template, config, table, items)
    return _validate_anchor_structure(config, report)


# ---------------------------------------------------------------- 表格模式


def _validate_table_structure(
    template: dict[str, Any], config: dict[str, Any], table: Any, items: list[dict]
) -> list[CheckResult]:
    if not items:
        detail = "明细表未重建成功：" + "、".join(table.issues or ["未识别到表头"])
        return [
            CheckResult(
                "table_structure",
                False,
                detail,
                severity=SEVERITY_ERROR,
                fields=(config.get("rows_field", "item_rows"),),
            )
        ]

    results: list[CheckResult] = [
        CheckResult(
            "table_structure",
            True,
            f"表格重建 {len(items)} 行（物理行 {table.physical_rows}）",
            fields=(config.get("rows_field", "item_rows"),),
        )
    ]

    # 必需列（模板 table.required_columns）：缺列说明表头识别不完整 → 结构不稳
    required_columns = [key for key in (template.get("table") or {}).get("required_columns", []) if key]
    missing_columns = [key for key in required_columns if key in table.columns and not table.columns[key].present]
    if missing_columns:
        titles = [table.columns[key].title or key for key in missing_columns]
        results.append(
            CheckResult(
                "table_columns",
                False,
                f"明细表缺少必需列：{'、'.join(titles)}",
                severity=SEVERITY_ERROR,
            )
        )
    elif required_columns:
        results.append(CheckResult("table_columns", True, "必需列齐全"))

    # 每条明细的关键单元格（模板 structure.item_table.required_cells）
    for key in config.get("required_cells") or []:
        missing_rows = [str(item.get("row_index")) for item in items if not item.get(key)]
        results.append(
            CheckResult(
                f"item_cell_{key}",
                not missing_rows,
                "" if not missing_rows else f"第 {'、'.join(missing_rows)} 行缺少 {key}",
                severity=SEVERITY_WARNING,
            )
        )
    return results


# ---------------------------------------------------------------- 锚点模式（旧逻辑）


def _validate_anchor_structure(config: dict[str, Any], report: Any) -> list[CheckResult]:
    rows_field = config.get("rows_field", "item_rows")
    rows = _int_value(report, rows_field)
    if rows is None:
        # 行数没提到 → 明细表结构无从判断；字段级校验已会报告该字段本身的问题
        return [
            CheckResult(
                "item_rows",
                True,
                f"明细行数（{rows_field}）未提取到，跳过结构校验",
                skipped=True,
                fields=(rows_field,),
            )
        ]
    if rows == 0:
        return [
            CheckResult(
                "item_rows",
                True,
                "未识别到明细行，跳过结构校验",
                skipped=True,
                fields=(rows_field,),
            )
        ]

    results: list[CheckResult] = [
        CheckResult("item_rows", True, f"明细 {rows} 行", fields=(rows_field,))
    ]
    required_columns = set(config.get("required_columns") or [])

    for label, field_name in (config.get("column_fields") or {}).items():
        count = _int_value(report, field_name)
        if count is None:
            results.append(
                CheckResult(
                    f"item_count_{field_name}",
                    True,
                    f"{label}列行数（{field_name}）未提取到，跳过该列校验",
                    skipped=True,
                    fields=(field_name,),
                )
            )
            continue
        passed = count == rows
        results.append(
            CheckResult(
                f"item_count_{field_name}",
                passed,
                f"明细 {rows} 行，{label}列提取 {count} 行"
                if not passed
                else f"{label}列 {count} 行与明细行数一致",
                severity=SEVERITY_ERROR if label in required_columns else SEVERITY_WARNING,
                fields=(rows_field, field_name),
            )
        )

    for field_name in config.get("first_row_fields") or []:
        value = _raw_value(report, field_name)
        results.append(
            CheckResult(
                f"item_first_row_{field_name}",
                bool(value),
                "" if value else f"明细 {rows} 行，但首行字段 {field_name} 为空",
                severity=SEVERITY_ERROR,
                fields=(field_name,),
            )
        )

    return results
