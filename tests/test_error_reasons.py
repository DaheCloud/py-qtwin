"""失败原因收集测试：解析失败/待人工确认时 doc.error_reason 应给出可读原因。

运行：.venv/Scripts/python.exe -m pytest tests/test_error_reasons.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.db import get_engine, init_db, make_session_factory
from services.pdf_service import PdfService

PAGE_W, PAGE_H = 595, 842


def _make_good_pdf(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    page.insert_text((80, 60), "销售合同", fontsize=20, fontname="china-s")
    page.insert_text((80, 120), "合同编号：", fontsize=12, fontname="china-s")
    page.insert_text((165, 120), "HT20260901", fontsize=12)
    page.insert_text((80, 170), "客户名称：", fontsize=12, fontname="china-s")
    page.insert_text((165, 170), "ABC有限公司", fontsize=12, fontname="china-s")
    page.insert_text((400, 320), "12800.00", fontsize=12)
    page.insert_text((400, 370), "2026-09-01", fontsize=12)
    doc.save(path)
    doc.close()


def _make_broken_pdf(path: Path) -> None:
    """坏值版式：contract_no 不是 HT+8 位数字，sign_date 是乱文本；
    日期行还撞了业务规则的位置。amount 区域留空。"""
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    page.insert_text((80, 60), "销售合同", fontsize=20, fontname="china-s")
    page.insert_text((80, 120), "合同编号：", fontsize=12, fontname="china-s")
    page.insert_text((165, 120), "XX-123", fontsize=12)
    page.insert_text((80, 170), "客户名称：", fontsize=12, fontname="china-s")
    page.insert_text((165, 170), "ABC有限公司", fontsize=12, fontname="china-s")
    # amount rect (400,300,550,330) 内不放内容
    page.insert_text((400, 370), "不是日期的文本", fontsize=12, fontname="china-s")
    doc.save(path)
    doc.close()


def _service(tmp_path: Path, template_engine) -> tuple[PdfService, object]:
    engine = get_engine(tmp_path / "t.db")
    init_db(engine)
    factory = make_session_factory(engine)
    return PdfService(template_engine), factory


def test_success_has_no_error_reason(tmp_path, contract_engine):
    pdf = tmp_path / "good.pdf"
    _make_good_pdf(pdf)
    service, factory = _service(tmp_path, contract_engine)
    with factory() as session:
        doc = service.process_document(session, str(pdf), None)
        assert doc.status == "success"
        assert doc.error_reason is None


def test_failed_document_collects_reasons(tmp_path, contract_engine):
    pdf = tmp_path / "broken.pdf"
    _make_broken_pdf(pdf)
    service, factory = _service(tmp_path, contract_engine)
    with factory() as session:
        doc = service.process_document(session, str(pdf), None)
        assert doc.status in ("failed", "manual_review")
        assert doc.error_reason, "失败文档必须有原因"

        text = doc.error_reason
        # 每个失败字段都应出现：字段名 + 错误 + 原始值提示
        assert "contract_no" in text and "XX-123" in text, text
        assert "amount" in text, text  # 空区域
        assert "sign_date" in text, text
        # 原因应是分号分隔的多条
        assert "；" in text


def test_duplicate_rejects_with_reason(tmp_path, contract_engine):
    pdf = tmp_path / "dup.pdf"
    _make_good_pdf(pdf)
    service, factory = _service(tmp_path, contract_engine)
    with factory() as session:
        service.process_document(session, str(pdf), None)
    with factory() as session:
        try:
            service.process_document(session, str(pdf), None)
            raise AssertionError("重复导入应抛错")
        except ValueError as exc:
            assert "已经导入" in str(exc)
