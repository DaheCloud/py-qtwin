"""整页文本辅助校验（pdf/text_audit.py）测试。

覆盖：
  · strip_ws / missing_values 纯函数语义（含 synth 字段跳过、raw 而非 normalized）
  · A 层：无文本层（扫描件）→ needs_ocr 路由状态（非业务失败），不再逐字段报锚点错误
  · B 层：正常发票（含折行项目名称）不产生取值存在性误报

运行：.venv/Scripts/python.exe -m pytest tests/test_text_audit.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.db import get_engine, init_db, make_session_factory
from pdf import text_audit
from pdf.template_engine import TemplateEngine
from pdf.validators import FieldResult
from services.pdf_service import PdfService
from tests.test_invoice_multiline import _make_invoice

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
PAGE_W, PAGE_H = 595.32, 841.92


class _Report:
    """最小 report 替身：missing_values 只用到 .fields。"""

    def __init__(self, fields: dict) -> None:
        self.fields = fields


def test_strip_ws_removes_spacing():
    assert text_audit.strip_ws("118812. 57") == "118812.57"
    assert text_audit.strip_ws("金 额\u3000x") == "金额x"
    assert text_audit.strip_ws(None) == ""


def test_missing_values_flags_absent_value():
    """值能在原文（去空白）找到即通过；找不到则报缺失；合成字段不参与。"""
    template = {
        "fields": {
            "amount": {"page": 0},
            "item_rows": {"page": 0, "below_mode": "count"},
        }
    }
    report = _Report(
        {
            "amount": FieldResult("amount", "118812. 57", "118812.57"),
            "item_rows": FieldResult("item_rows", "3", "3"),
        }
    )

    assert text_audit.missing_values(template, report, ["合计 118812. 57"]) == []
    assert text_audit.missing_values(template, report, ["合计 99999.99"]) == [
        ("amount", "118812.57")
    ]


def test_missing_values_uses_raw_value():
    """日期用 raw（2026年09月09日）而非 normalized（2026-09-09）比对。"""
    template = {"fields": {"invoice_date": {"page": 0}}}
    report = _Report(
        {"invoice_date": FieldResult("invoice_date", "2026年09月09日", "2026-09-09")}
    )

    assert text_audit.missing_values(template, report, ["开票日期： 2026年 09月 09日"]) == []


def _make_scanned_pdf(path: Path) -> None:
    """无文本层的"扫描件"：只有图形，没有任何文字。"""
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    page.draw_rect(
        pymupdf.Rect(40, 40, PAGE_W - 40, PAGE_H - 40), color=(0, 0, 0), fill=(0.85, 0.85, 0.85)
    )
    doc.save(path)
    doc.close()


def _service() -> tuple[PdfService, object]:
    db_engine = get_engine(":memory:")
    init_db(db_engine)
    return PdfService(TemplateEngine(TEMPLATES_DIR)), make_session_factory(db_engine)


def test_scanned_pdf_routed_to_needs_ocr(tmp_path):
    """无文本层是**路由条件**而非业务失败：→ needs_ocr 状态（等待 OCR）。

    OCR 接入后的语义：needs_ocr → OCR → 成功继续解析 / OCR 失败才 failed
    （失败原因记 OCR 失败，而不是"无文本层"）。当前先以独立状态挂起，
    与业务状态机的 failed 区分开。
    """
    pdf = tmp_path / "scanned.pdf"
    _make_scanned_pdf(pdf)
    service, factory = _service()

    with factory() as session:
        doc = service.process_document(session, str(pdf))
        status, reason, fields = doc.status, doc.error_reason, list(doc.fields)

    assert status == "needs_ocr"
    assert "无文本层" in (reason or "") and "OCR" in (reason or "")
    assert fields == []


def test_normal_invoice_has_no_advisory(tmp_path):
    """正常发票（含折成三行的项目名称）不应产生取值存在性误报。"""
    pdf = tmp_path / "invoice_ok.pdf"
    _make_invoice(pdf, rows=1, wrap_project_name=True, wrap_line_gap=16.0)
    service, factory = _service()

    with factory() as session:
        doc = service.process_document(session, str(pdf))
        status, reason = doc.status, doc.error_reason

    assert status == "success", reason
    assert reason is None, f"不应有提示：{reason}"
