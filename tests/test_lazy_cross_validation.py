"""Lazy Cross Validation（方案 §26-§28）+ precheck + 三维置信度测试。

覆盖：
  · 高可信发票 → 不启动第二引擎（doc.verifications 为空）→ success（方案 §28 优点）
  · 兜底模板（识别置信度低）→ 自动触发第二引擎
  · cross_verify=True 强制 / False 跳过（调用方显式控制）
  · 触发判定 need_secondary_engine 与 validation/overall 置信度（方案 §31）
  · precheck：扫描件 / 损坏 PDF 的快速失败（方案 §2/§30）

运行：.venv/Scripts/python.exe -m pytest tests/test_lazy_cross_validation.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.db import get_engine, init_db, make_session_factory
from pdf.confidence import need_secondary_engine, overall_confidence, validation_confidence
from pdf.precheck import precheck_document
from pdf.template_engine import TemplateEngine
from services.pdf_service import PdfService
from tests.test_generic_fallback import _make_legacy_invoice
from tests.test_invoice_multiline import _make_invoice

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"


def _service() -> tuple[PdfService, object]:
    db_engine = get_engine(":memory:")
    init_db(db_engine)
    return PdfService(TemplateEngine(TEMPLATES_DIR)), make_session_factory(db_engine)


# ------------------------------------------------- Lazy Cross Validation


class TestLazyCrossValidation:
    def test_clean_invoice_skips_secondary_engine(self, tmp_path):
        """高可信发票：识别可靠 + 字段完整 + 校验通过 → 不跑 pdfplumber（方案 §28）。"""
        pdf = tmp_path / "clean.pdf"
        _make_invoice(pdf, rows=1)
        service, factory = _service()

        with factory() as session:
            doc = service.process_document(session, str(pdf))
            verifications = list(doc.verifications)
            status, reason = doc.status, doc.error_reason

        assert status == "success", reason
        assert verifications == [], "高可信发票不应触发第二引擎"
        assert doc.parse_confidence >= 90
        assert doc.overall_confidence is not None

    def test_fallback_triggers_secondary_automatically(self, tmp_path):
        """兜底模板：识别置信度低 → 自动触发第二引擎（方案 §27）。"""
        pdf = tmp_path / "legacy.pdf"
        _make_legacy_invoice(pdf)
        service, factory = _service()

        with factory() as session:
            doc = service.process_document(session, str(pdf))
            verifications = [(v.field_name, v.matched) for v in doc.verifications]
            status, reason, identify_conf = doc.status, doc.error_reason, doc.identify_confidence

        assert status == "manual_review", reason
        assert identify_conf < 85
        assert verifications, "识别置信度低于阈值时应自动触发第二引擎"
        assert all(matched for _, matched in verifications)

    def test_forced_cross_verify(self, tmp_path):
        """cross_verify=True：调用方强制执行（复核场景）。"""
        pdf = tmp_path / "clean.pdf"
        _make_invoice(pdf, rows=1)
        service, factory = _service()

        with factory() as session:
            doc = service.process_document(session, str(pdf), cross_verify=True)
            verifications = [(v.field_name, v.matched) for v in doc.verifications]

        assert verifications
        assert all(matched for _, matched in verifications)

    def test_cross_verify_false_skips_even_for_fallback(self, tmp_path):
        """cross_verify=False：调用方显式跳过（性能优先场景）。"""
        pdf = tmp_path / "legacy.pdf"
        _make_legacy_invoice(pdf)
        service, factory = _service()

        with factory() as session:
            doc = service.process_document(session, str(pdf), cross_verify=False)
            verifications = list(doc.verifications)
            status, reason = doc.status, doc.error_reason

        assert status == "manual_review", reason
        assert verifications == []


# ------------------------------------------------- 触发判定与置信度


class TestSecondaryEnginePolicy:
    def test_high_confidence_does_not_trigger(self):
        assert not need_secondary_engine()

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"identify_conf": 84},
            {"parse_conf": 89},
            {"structure_issues": 1},
            {"business_issues": 1},
            {"required_missing": 1},
            {"critical_failed": 1},
            {"table_failed": True},
        ],
    )
    def test_risk_conditions_trigger(self, kwargs):
        assert need_secondary_engine(**kwargs)

    def test_validation_and_overall_confidence(self):
        assert validation_confidence() == 100
        dirty = validation_confidence(business_failures=1, structure_issues=1, cross_mismatch=1)
        assert dirty == 100 - 30 - 15 - 20

        assert overall_confidence(100, 100, 100) == 100
        assert overall_confidence(0, 0, 0) == 0
        # 识别 30% + 解析 40% + 校验 30%
        assert overall_confidence(80, 100, 100) == 94


# ------------------------------------------------- precheck（方案 §2/§30）


class TestPrecheck:
    def test_scanned_pdf_detected(self, tmp_path):
        """无文本层（只有图形）→ needs_ocr。"""
        pdf = tmp_path / "scanned.pdf"
        doc = pymupdf.open()
        page = doc.new_page(width=595.32, height=841.92)
        page.draw_rect(pymupdf.Rect(60, 60, 500, 400))
        doc.save(pdf)
        doc.close()

        pre = precheck_document(str(pdf))

        assert pre.ok and pre.page_count == 1
        assert pre.char_count == 0
        assert pre.needs_ocr

    def test_text_pdf_not_flagged(self, tmp_path):
        pdf = tmp_path / "text.pdf"
        _make_invoice(pdf, rows=1)

        pre = precheck_document(str(pdf))

        assert pre.ok and pre.has_text and not pre.needs_ocr
        assert pre.as_dict()["page_size"] == [595.32, 841.92]

    def test_scanned_first_page_with_text_second_page(self, tmp_path):
        """首页是扫描图/图片页、次页有文本 → 不误判为扫描件挂起 OCR（多页判定）。"""
        pdf = tmp_path / "cover_scan.pdf"
        doc = pymupdf.open()
        page1 = doc.new_page(width=595.32, height=841.92)
        page1.draw_rect(pymupdf.Rect(60, 60, 500, 400))
        page2 = doc.new_page(width=595.32, height=841.92)
        page2.insert_text((60, 60), "电子发票（普通发票）", fontsize=16, fontname="china-s")
        doc.save(pdf)
        doc.close()

        pre = precheck_document(str(pdf))

        assert pre.ok and pre.has_text and not pre.needs_ocr

    def test_broken_pdf_reported(self, tmp_path):
        path = tmp_path / "broken.pdf"
        path.write_bytes(b"this is not a pdf at all")

        pre = precheck_document(str(path))

        assert not pre.ok
        assert pre.error

    def test_service_fails_fast_for_broken_pdf(self, tmp_path):
        """损坏 PDF：预检查直接 failed 落库，不抛异常也不进解析链路。"""
        path = tmp_path / "broken.pdf"
        path.write_bytes(b"junk")
        service, factory = _service()

        with factory() as session:
            doc = service.process_document(session, str(path))

        assert doc.status == "failed"
        assert "无法打开" in (doc.error_reason or "")
