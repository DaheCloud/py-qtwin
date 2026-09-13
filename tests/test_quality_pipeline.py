"""方案落地回归：模板识别评分 / 状态机 / 结构校验 / 业务数学校验 / 置信度 / scope。

覆盖方案（PDF 发票识别方案优化设计）的六个阶段：
  §3  模板识别打分（must / any / exclude / priority / min_score），不再依赖文件名顺序
  §5  dynamic_parser 的 scope 取值分区
  §6/§11 字段 required / critical 与状态机
  §7  结构校验（多行明细不再直接转人工，结构异常才转）
  §8  业务数学校验（Decimal + 容差，尾差转人工、明显不成立判失败）
  §10 规则式置信度评分

运行：.venv/Scripts/python.exe -m pytest tests/test_quality_pipeline.py -v
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pdf.confidence import identify_confidence, parse_confidence
from pdf.dynamic_parser import _Word, extract_anchor_field
from pdf.template_engine import IdentifyResult, TemplateEngine, evaluate_template
from pdf.validators import (
    SEVERITY_ERROR,
    CheckResult,
    FieldResult,
    SEVERITY_OPTIONAL,
    SEVERITY_REQUIRED,
    field_severity,
    validate_business,
    validate_structure,
)
from services.pdf_service import (
    STATUS_FAILED,
    STATUS_MANUAL_REVIEW,
    STATUS_SUCCESS,
    determine_status,
)


class _Report:
    """最小解析报告替身：只需要 fields 字典。"""

    def __init__(self, fields: dict[str, FieldResult]):
        self.fields = fields


def _field(name: str, value: str | None) -> FieldResult:
    return FieldResult(field_name=name, raw_value=value or "", normalized_value=value)


# ----------------------------------------------------------- §3 模板识别打分


def test_evaluate_template_must_any_exclude_min_score():
    text = "电子发票（增值税专用发票）  建筑服务发生地  建筑项目名称"
    template = {
        "template": "t",
        "identify": {
            "priority": 100,
            "must": ["建筑服务发生地", "建筑项目名称"],
            "any": ["电子发票", "增值税专用发票"],
            "exclude": [],
            "min_score": 20,
        },
    }
    # must 2 条 ×10 + any 2 条 ×2 + priority 100×0.001 = 24.1
    assert evaluate_template(text, template) == 24.1

    # must 缺一 → 直接淘汰
    assert evaluate_template("建筑服务发生地", template) is None
    # exclude 命中 → 淘汰
    assert evaluate_template(text, {**template, "identify": {**template["identify"], "exclude": ["建筑项目名称"]}}) is None
    # 分数低于 min_score → 淘汰
    assert evaluate_template("电子发票", template) is None
    # 一个关键词都没命中 → 不适用
    assert evaluate_template("随便什么文本", {**template, "identify": {"any": ["发票"]}}) is None


def test_identify_picks_highest_score_not_filename_order(tmp_path):
    """同时命中多个模板时取分数最高者：文件名排序不再参与业务识别。"""
    (tmp_path / "aaa_any.json").write_text(
        json.dumps(
            {
                "template": "aaa_any",
                "mode": "dynamic",
                "identify": {"priority": 1, "any": ["电子发票"], "min_score": 2},
                "fields": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "zzz_must.json").write_text(
        json.dumps(
            {
                "template": "zzz_must",
                "mode": "dynamic",
                "identify": {
                    "priority": 1,
                    "must": ["建筑服务发生地"],
                    "any": ["电子发票"],
                    "min_score": 10,
                },
                "fields": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    import pymupdf

    pdf = tmp_path / "invoice.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595.32, height=841.92)
    page.insert_text((60, 60), "电子发票  建筑服务发生地", fontsize=12, fontname="china-s")
    doc.save(pdf)
    doc.close()

    result = TemplateEngine(tmp_path).identify(str(pdf))

    assert result.mode == "match"
    assert result.template_id == "zzz_must"  # 分数 12.001 > 2.001，尽管文件名更靠后
    assert result.matched["must"] == ["建筑服务发生地"]
    assert [tid for tid, _ in result.candidates] == ["zzz_must", "aaa_any"]


def test_identify_fallback_and_none(tmp_path):
    (tmp_path / "fallback.json").write_text(
        json.dumps(
            {
                "template": "fb",
                "mode": "dynamic",
                "identify": {"fallback": True, "fallback_detect": ["发票"]},
                "fields": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    import pymupdf

    def make(name: str, text: str) -> str:
        path = tmp_path / name
        doc = pymupdf.open()
        page = doc.new_page(width=595.32, height=841.92)
        page.insert_text((60, 60), text, fontsize=12, fontname="china-s")
        doc.save(path)
        doc.close()
        return str(path)

    assert TemplateEngine(tmp_path).identify(make("a.pdf", "电子发票")).mode == "fallback"
    empty = TemplateEngine(tmp_path / "empty").identify(make("b.pdf", "合同"))
    assert empty.mode == "none" and empty.template is None


# ----------------------------------------------------------- §5 scope 取值分区


def test_scope_limits_candidate_region_not_anchor():
    """scope 只限制候选取值区间：同名列重复出现时只取区间内的那个。

    锚点"金额"在两处出现（表头上、备注里），scope 用起始/结束标签把取值框在
    明细区间内，既不影响锚点查找，也不会取到区间外的同名值。
    """
    words = [
        _Word(60, 100, 120, 110, "金额"),        # 表头锚点
        _Word(60, 130, 130, 140, "¥100.00"),    # 明细区内的值（应命中）
        _Word(60, 200, 120, 210, "合计"),        # 结束标签
        _Word(60, 230, 130, 240, "¥999.00"),    # 结束标签之后：不得命中
    ]
    spec = {
        "anchor": "金额",
        "direction": "below",
        "max_distance": 800,
        "below_mode": "row",
        "pick": "last",
        "type": "decimal",
        "pattern": r"^[¥￥]?[\d,]+\.\d{2}$",
        "scope": {"end_anchor": ["合计"]},
    }

    result = extract_anchor_field("amount", words, spec)

    assert result.valid, result.errors
    assert result.normalized_value == "100.00"


def test_scope_ignores_missing_anchor_labels():
    """范围标签不存在时退回全页，规则不会整体失效。"""
    words = [
        _Word(60, 100, 120, 110, "金额"),
        _Word(60, 130, 130, 140, "¥100.00"),
    ]
    spec = {
        "anchor": "金额",
        "direction": "below",
        "max_distance": 800,
        "below_mode": "row",
        "type": "decimal",
        "pattern": r"^[¥￥]?[\d,]+\.\d{2}$",
        "scope": {"start_anchor": ["项目名称"], "end_anchor": ["价税合计"]},
    }

    result = extract_anchor_field("amount", words, spec)

    assert result.valid and result.normalized_value == "100.00"


def test_letter_spaced_header_matched_by_merged_anchor():
    """字间距把表头切成"金"+"额"时，候选锚点"金额"靠相邻词拼接仍能命中。"""
    words = [
        _Word(405.4, 151.1, 414.4, 160.1, "金"),
        _Word(423.4, 151.1, 432.4, 160.1, "额"),
        _Word(446.5, 151.1, 496.1, 160.1, "税率/征收率"),
        _Word(393.2, 160.9, 433.7, 169.9, "118812.57"),
    ]
    spec = {
        "anchor": ["金额", "金 额"],
        "anchor_span": True,
        "direction": "below",
        "max_distance": 800,
        "below_mode": "row",
        "pattern": r"^[¥￥]?[\d,]+\.\d{2}$",
    }

    result = extract_anchor_field("amount", words, spec)

    assert result.valid, result.errors
    assert result.normalized_value == "118812.57"


# ----------------------------------------------------------- §6 字段严重程度


def test_field_severity_levels():
    assert field_severity({"optional": True}) == SEVERITY_OPTIONAL
    assert field_severity({}) == SEVERITY_REQUIRED
    assert field_severity({"critical": True}) == "critical"
    # optional 优先于 critical（可选字段缺失不应判失败）
    assert field_severity({"optional": True, "critical": True}) == SEVERITY_OPTIONAL


# ----------------------------------------------------------- §11 状态机


def test_status_machine_priority():
    # critical 字段失败 / 关键业务关系不成立 → failed
    assert determine_status(critical_failed=1) == STATUS_FAILED
    assert determine_status(business_errors=1) == STATUS_FAILED
    # 兜底模板 / 必填失败 / 结构异常 / 引擎不一致 / 业务尾差 → manual_review
    assert determine_status(identify_mode="fallback") == STATUS_MANUAL_REVIEW
    assert determine_status(required_failed=1) == STATUS_MANUAL_REVIEW
    assert determine_status(structure_issues=1) == STATUS_MANUAL_REVIEW
    assert determine_status(cross_mismatch=1) == STATUS_MANUAL_REVIEW
    assert determine_status(business_warnings=1) == STATUS_MANUAL_REVIEW
    assert determine_status(parse_confidence=69) == STATUS_MANUAL_REVIEW
    # 都没有 → success
    assert determine_status(parse_confidence=100) == STATUS_SUCCESS


# ----------------------------------------------------------- §7 结构校验


def _item_report(rows: str | None, amount_rows: str | None, tax_rows: str | None, **extra):
    fields = {
        "item_rows": _field("item_rows", rows),
        "item_amount_rows": _field("item_amount_rows", amount_rows),
        "item_tax_rows": _field("item_tax_rows", tax_rows),
        "item_name": _field("item_name", "*建筑服务*工程服务"),
        "amount": _field("amount", "118812.57"),
    }
    for name, value in extra.items():
        fields[name] = _field(name, value)
    return _Report(fields)


STRUCTURE_TEMPLATE = {
    "structure": {
        "item_table": {
            "rows_field": "item_rows",
            "column_fields": {"金额": "item_amount_rows", "税额": "item_tax_rows"},
            "required_columns": ["金额", "税额"],
            "first_row_fields": ["item_name", "amount"],
        }
    }
}


def test_structure_consistent_multi_row_passes():
    checks = validate_structure(STRUCTURE_TEMPLATE, _item_report("2", "2", "2"))
    assert all(check.passed for check in checks), [c.as_dict() for c in checks]


def test_structure_column_count_mismatch_is_issue():
    """明细 2 行但金额列只提到 1 行 → 结构异常（金额列是强条件 → error 级）。"""
    checks = validate_structure(STRUCTURE_TEMPLATE, _item_report("2", "1", "2"))
    failed = [c for c in checks if c.failed]
    assert [c.rule for c in failed] == ["item_count_item_amount_rows"]
    assert failed[0].severity == SEVERITY_ERROR
    assert "明细 2 行" in failed[0].detail and "金额列提取 1 行" in failed[0].detail


def test_structure_first_row_field_missing_is_issue():
    checks = validate_structure(
        STRUCTURE_TEMPLATE, _item_report("1", "1", "1", item_name=None)
    )
    failed = [c for c in checks if c.failed]
    assert [c.rule for c in failed] == ["item_first_row_item_name"]


def test_structure_skipped_when_no_rows():
    checks = validate_structure(STRUCTURE_TEMPLATE, _item_report(None, None, None))
    assert all(check.skipped for check in checks)


# ----------------------------------------------------------- §8 业务数学校验


BUSINESS_TEMPLATE = {
    "business_check": {
        "rows_field": "item_rows",
        "item": {
            "name": "item_name",
            "quantity": "quantity",
            "unit_price": "unit_price",
            "amount": "amount",
            "tax_rate": "tax_rate",
            "tax": "tax_amount",
        },
        "totals": {"amount": "amount", "tax": "tax_amount", "grand": "total_amount"},
    }
}


def _business_report(**values) -> _Report:
    defaults = {
        "item_rows": "1",
        "quantity": "1",
        "unit_price": "73933.20",
        "amount": "73933.20",
        "tax_rate": "3%",
        "tax_amount": "2218.00",
        "total_amount": "76151.20",
    }
    defaults.update({k: v for k, v in values.items()})
    return _Report({name: _field(name, value) for name, value in defaults.items()})


def test_business_checks_pass_for_consistent_invoice():
    checks = validate_business(BUSINESS_TEMPLATE, _business_report())
    assert all(check.passed for check in checks), [c.as_dict() for c in checks]
    rules = {c.rule for c in checks}
    assert rules == {"item_amount", "item_tax", "grand_total"}


def test_business_grand_total_gross_mismatch_is_error():
    """价税合计 200.00 ≠ 金额+税额 118812.57+10693.13 → 明显不成立 → error（判 failed）。"""
    checks = validate_business(
        BUSINESS_TEMPLATE, _business_report(amount="118812.57", tax_amount="10693.13", total_amount="200.00")
    )
    grand = next(c for c in checks if c.rule == "grand_total")
    assert grand.failed and grand.severity == SEVERITY_ERROR


def test_business_grand_total_cent_difference_is_warning():
    """分位尾差（0.01 元）在容差内直接通过；超出容差但不算离谱 → warning（转人工）。"""
    ok = validate_business(BUSINESS_TEMPLATE, _business_report(total_amount="76151.21"))
    assert next(c for c in ok if c.rule == "grand_total").passed

    checks = validate_business(BUSINESS_TEMPLATE, _business_report(total_amount="76150.50"))
    grand = next(c for c in checks if c.rule == "grand_total")
    assert grand.failed and grand.severity != SEVERITY_ERROR


def test_business_item_math_detects_wrong_amount():
    checks = validate_business(BUSINESS_TEMPLATE, _business_report(unit_price="100.00"))
    item = next(c for c in checks if c.rule == "item_amount")
    assert item.failed and item.severity == SEVERITY_ERROR


def test_business_skips_item_math_for_multiple_rows():
    """多行明细未逐行提取：明细级校验记 skipped（留痕不误报），合计校验照做。"""
    checks = validate_business(BUSINESS_TEMPLATE, _business_report(item_rows="3"))
    skipped = {c.rule for c in checks if c.skipped}
    assert {"item_amount", "sum_amount", "sum_tax"} <= skipped
    grand = next(c for c in checks if c.rule == "grand_total")
    assert grand.passed and not grand.skipped


def test_business_skips_tax_check_for_tax_free_rate():
    """免税/不征税（税率不可解析）→ 跳过 金额×税率，不误报。"""
    checks = validate_business(BUSINESS_TEMPLATE, _business_report(tax_rate="免税", tax_amount=None))
    item = next(c for c in checks if c.rule == "item_tax")
    assert item.skipped


# ----------------------------------------------------------- §10 置信度


def test_identify_confidence_by_mode():
    match = IdentifyResult(template={"template": "t"}, mode="match", matched={"must": ["a"]})
    any_only = IdentifyResult(template={"template": "t"}, mode="match", matched={"any": ["a"]})
    fallback = IdentifyResult(template={"template": "g"}, mode="fallback")
    none = IdentifyResult(template=None, mode="none")

    assert identify_confidence(match) == 100
    assert identify_confidence(any_only) == 90
    assert identify_confidence(fallback) == 60
    assert identify_confidence(none) == 0


def test_parse_confidence_deductions_and_clamp():
    clean = IdentifyResult(template={"template": "t"}, mode="match", matched={"must": ["a"]})
    assert parse_confidence(identify=clean) == 100

    dirty = parse_confidence(
        identify=IdentifyResult(template={"template": "g"}, mode="fallback"),
        cross_mismatch=1,
        structure_issues=1,
        business_failures=1,
        required_missing=1,
        optional_missing=2,
    )
    # 100 - 20(fallback) - 10(识别非高置信) - 20 - 15 - 30 - 10 - 4 = 0（下限截断）
    assert dirty == 0


# ----------------------------------------------------------- 校验结论序列化


def test_check_result_serialisation_skips_empty_fields():
    result = CheckResult("item_amount", True, "数量×单价=100.00 vs 金额=100.00", fields=("quantity",))
    assert result.as_dict() == {
        "rule": "item_amount",
        "passed": True,
        "severity": "warning",
        "detail": "数量×单价=100.00 vs 金额=100.00",
        "fields": ["quantity"],
    }
    assert Decimal("100.00") == Decimal("100.00")
