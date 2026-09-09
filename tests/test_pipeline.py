"""端到端冒烟测试：生成符合 contract_v1 模板坐标的样例 PDF，跑通完整管线。

运行：.venv/Scripts/python.exe -m pytest tests/ -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.db import get_engine, init_db, make_session_factory
from pdf.template_engine import TemplateEngine
from pdf.validators import FieldResult, validate_business_rules, validate_field
from services.pdf_service import PdfService, file_sha256

PAGE_W, PAGE_H = 595, 842


def _make_pdf(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    # china-s：内置中文字体；默认 Helvetica 无法编码中文
    page.insert_text((80, 60), "销售合同", fontsize=20, fontname="china-s")
    # 与 templates/contract_v1.json 的 rect 对齐（插入点为文字基线，rect 上边需留出字高）
    # 标签在 x=80，值在 x=165 —— 值单独落在 rect 内，模拟真实版式
    page.insert_text((80, 120), "合同编号：", fontsize=12, fontname="china-s")
    page.insert_text((165, 120), "HT20260901", fontsize=12)  # contract_no rect (165,100,320,130)
    page.insert_text((80, 170), "客户名称：", fontsize=12, fontname="china-s")
    page.insert_text((165, 170), "ABC有限公司", fontsize=12, fontname="china-s")  # customer_name rect (165,150,350,180)
    page.insert_text((400, 320), "12800.00", fontsize=12)  # amount rect (400,300,550,330)
    page.insert_text((400, 370), "2026-09-01", fontsize=12)  # sign_date rect (400,350,550,380)
    doc.save(path)
    doc.close()


def test_fixed_region_parse_and_cross_verify(tmp_path):
    pdf_path = tmp_path / "contract001.pdf"
    _make_pdf(pdf_path)

    engine = TemplateEngine("templates")
    tpl = engine.detect(str(pdf_path))
    assert tpl is not None, "模板识别失败"
    assert tpl["template"] == "contract_v1"

    from pdf.pdfplumber_validator import PdfplumberValidator
    from pdf.pymupdf_parser import FixedRegionParser

    report = FixedRegionParser().parse(str(pdf_path), tpl)
    assert report.valid, f"字段校验失败：{[(k, v.errors) for k, v in report.fields.items() if not v.valid]}"
    assert report.normalized == {
        "contract_no": "HT20260901",
        "customer_name": "ABC有限公司",
        "amount": "12800.00",
        "sign_date": "2026-09-01",
    }

    vreport = PdfplumberValidator().verify(str(pdf_path), tpl, report)
    assert vreport.all_matched, f"交叉验证不一致：{vreport.mismatches}"


def test_business_rules(tmp_path):
    fields = {
        "amount": FieldResult("amount", "12800.00", "12800.00"),
        "customer_name": FieldResult("customer_name", "ABC有限公司", "ABC有限公司"),
    }
    rules = [
        {"field": "amount", "op": ">=", "value": "0"},
        {"field": "customer_name", "op": "not_empty"},
    ]
    assert validate_business_rules(fields, rules) == []


def test_service_persists_in_transaction(tmp_path, monkeypatch):
    # 每个测试用独立 SQLite 文件
    db_path = tmp_path / "app.db"
    engine = get_engine(db_path)
    init_db(engine)
    factory = make_session_factory(engine)

    pdf_path = tmp_path / "contract002.pdf"
    _make_pdf(pdf_path)

    service = PdfService(TemplateEngine("templates"))
    with factory() as session:
        doc = service.process_document(session, str(pdf_path))
        assert doc.status == "success"
        assert {f.field_name: f.normalized_value for f in doc.fields} == {
            "contract_no": "HT20260901",
            "customer_name": "ABC有限公司",
            "amount": "12800.00",
            "sign_date": "2026-09-01",
        }
        assert all(v.matched for v in doc.verifications)

        # 去重：同一 hash 再次导入应抛错
        import pytest

        with pytest.raises(ValueError, match="已经导入"):
            service.process_document(session, str(pdf_path))
