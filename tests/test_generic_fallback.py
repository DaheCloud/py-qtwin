"""通用兜底模板（模板指纹未命中 → 通用锚点解析）测试。

覆盖流程：
  PDF → 检测模板指纹 → 已知模板？是 → 专属模板解析
                                 否 → invoice_generic 通用锚点解析
                                     （连兜底都不适用时才报错）

运行：.venv/Scripts/python.exe -m pytest tests/test_generic_fallback.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.db import get_engine, init_db, make_session_factory
from pdf.dynamic_parser import _Word, extract_anchor_field
from pdf.template_engine import TemplateEngine
from services.pdf_service import PdfService
from tests.test_invoice_multiline import _make_invoice

PAGE_W, PAGE_H = 595.32, 841.92
TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"


def _make_legacy_invoice(path: Path) -> None:
    """仿真"增值税电子普通发票"版式。

    标题不含 invoice_v1 的 detect 关键词（"电子发票"被"电子普通发票"隔开），
    但发票要素齐全（购销方左右分栏、合计行），用于验证通用兜底解析。
    明细表头用"货物或应税劳务、服务名称"（专属模板锚点是"项目名称"，通用
    模板通过候选锚点列表覆盖），且不含建筑服务字段（走 optional 缺失路径）。
    """
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)

    page.insert_text((60, 60), "增值税电子普通发票", fontsize=16, fontname="china-s")
    page.insert_text((60, 90), "发票号码：", fontsize=10, fontname="china-s")
    page.insert_text((125, 90), "01234567", fontsize=10)
    page.insert_text((60, 110), "开票日期：", fontsize=10, fontname="china-s")
    page.insert_text((125, 110), "2026年03月05日", fontsize=10, fontname="china-s")

    page.insert_text((60, 160), "名称：", fontsize=10, fontname="china-s")
    page.insert_text((95, 160), "中磐建设集团有限公司", fontsize=10, fontname="china-s")
    page.insert_text((60, 178), "纳税人识别号：", fontsize=10, fontname="china-s")
    page.insert_text((135, 178), "914114005698015827", fontsize=10)

    page.insert_text((330, 160), "名称：", fontsize=10, fontname="china-s")
    page.insert_text((365, 160), "厦门仪翔建设工程有限公司", fontsize=10, fontname="china-s")
    page.insert_text((330, 178), "纳税人识别号：", fontsize=10, fontname="china-s")
    page.insert_text((405, 178), "913502000658790529", fontsize=10)

    header_y = 300
    page.insert_text((60, header_y), "货物或应税劳务、服务名称", fontsize=9, fontname="china-s")
    page.insert_text((195, header_y), "单 位", fontsize=9, fontname="china-s")
    page.insert_text((230, header_y), "数量", fontsize=9, fontname="china-s")
    page.insert_text((290, header_y), "单价", fontsize=9, fontname="china-s")
    page.insert_text((350, header_y), "金额", fontsize=9, fontname="china-s")
    page.insert_text((420, header_y), "税率/征收率", fontsize=9, fontname="china-s")
    page.insert_text((500, header_y), "税额", fontsize=9, fontname="china-s")

    row_y = header_y + 22
    page.insert_text((60, row_y), "*建筑服务*劳务工程款", fontsize=9, fontname="china-s")
    page.insert_text((195, row_y), "项", fontsize=9, fontname="china-s")
    page.insert_text((230, row_y), "1", fontsize=9)
    page.insert_text((290, row_y), "73933.20", fontsize=9)
    page.insert_text((350, row_y), "73933.20", fontsize=9)
    page.insert_text((420, row_y), "3%", fontsize=9)
    page.insert_text((500, row_y), "2218.00", fontsize=9)

    total_y = row_y + 40
    page.insert_text((60, total_y), "合 计", fontsize=9, fontname="china-s")
    page.insert_text((350, total_y), "¥73933.20", fontsize=9, fontname="china-s")
    page.insert_text((500, total_y), "¥2218.00", fontsize=9, fontname="china-s")
    page.insert_text((360, total_y + 30), "（小写）¥76151.20", fontsize=9, fontname="china-s")

    doc.save(path)
    doc.close()


def _service() -> tuple[PdfService, object]:
    db_engine = get_engine(":memory:")
    init_db(db_engine)
    return PdfService(TemplateEngine(TEMPLATES_DIR)), make_session_factory(db_engine)


# ------------------------------------------------- 三级模板选择（指纹识别）

def test_resolve_match_for_known_invoice(tmp_path):
    """建筑服务数电票（带信息块）→ invoice_v1 专属模板，不走兜底。"""
    pdf = tmp_path / "known_invoice.pdf"
    _make_invoice(pdf, rows=1)

    tpl, kind = TemplateEngine(TEMPLATES_DIR).resolve(str(pdf))

    assert kind == "match"
    assert tpl is not None and tpl["template"] == "invoice_v1"


def test_resolve_fallback_for_unknown_invoice(tmp_path):
    """标题未命中专属模板但包含"发票" → 通用兜底模板。"""
    pdf = tmp_path / "legacy_invoice.pdf"
    _make_legacy_invoice(pdf)

    tpl, kind = TemplateEngine(TEMPLATES_DIR).resolve(str(pdf))

    assert kind == "fallback"
    assert tpl is not None and tpl["template"] == "invoice_generic"


def test_resolve_none_for_non_invoice(tmp_path):
    """连"发票"都没有的文档（如合同）不套用发票兜底模板。"""
    pdf = tmp_path / "contract.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    page.insert_text((80, 60), "销售合同", fontsize=20, fontname="china-s")
    doc.save(pdf)
    doc.close()

    tpl, kind = TemplateEngine(TEMPLATES_DIR).resolve(str(pdf))

    assert tpl is None
    assert kind == "none"


def test_fallback_template_not_in_exclusive_detect(tmp_path):
    """兜底模板不参与 detect()：专属识别只认 invoice_v1。"""
    pdf = tmp_path / "legacy_invoice.pdf"
    _make_legacy_invoice(pdf)

    engine = TemplateEngine(TEMPLATES_DIR)

    assert engine.detect(str(pdf)) is None


def test_construction_invoice_prefers_v1_over_v2(tmp_path):
    """建筑服务信息块（v1 关键词）与发票标题（v2 关键词）同时命中时，v1 优先。"""
    pdf = tmp_path / "construction_invoice.pdf"
    _make_invoice(pdf, rows=1)

    tpl, kind = TemplateEngine(TEMPLATES_DIR).resolve(str(pdf))

    assert kind == "match"
    assert tpl is not None and tpl["template"] == "invoice_v1"


# ------------------------------------------------- 端到端：通用锚点解析

def test_generic_fallback_end_to_end(tmp_path):
    """未知版式发票：兜底模板完整解析 + 交叉验证一致。

    识别为 fallback 说明"没命中任何专属模板"，版式本身未经确认，因此即使字段
    全部解析成功也转人工复核（方案 §11.2：模板匹配置信度低）。
    """
    pdf = tmp_path / "legacy_invoice.pdf"
    _make_legacy_invoice(pdf)
    service, factory = _service()

    with factory() as session:
        doc = service.process_document(session, str(pdf))
        values = {f.field_name: f.normalized_value for f in doc.fields}
        matched = {v.field_name: v.matched for v in doc.verifications}
        status, reason, template_id = doc.status, doc.error_reason, doc.template_id

    assert template_id == "invoice_generic"
    assert status == "manual_review", reason
    assert "未识别到专属模板" in (reason or "")
    # 兜底识别：识别置信度打折，解析置信度再扣一档
    assert doc.identify_confidence == 60
    assert doc.parse_confidence < 100
    assert values["invoice_no"] == "01234567"
    assert values["invoice_date"] == "2026-03-05"
    assert values["buyer_name"] == "中磐建设集团有限公司"
    assert values["seller_name"] == "厦门仪翔建设工程有限公司"
    assert values["amount"] == "73933.20"
    assert values["tax_amount"] == "2218.00"
    assert values["total_amount"] == "76151.20"
    # 候选锚点列表：专属模板用"项目名称"，该版式用"货物或应税劳务"
    assert values["item_name"] == "*建筑服务*劳务工程款"
    assert values["tax_rate"] == "3%"
    # 带字间距的明细表头（"单 位/数 量/单 价"）与行数统计在兜底模板上同样生效
    assert values["unit"] == "项"
    assert values["quantity"] == "1"
    assert values["unit_price"] == "73933.20"
    assert values["item_rows"] == "1"
    # optional 字段：非建筑服务发票缺失，不影响整单成败
    assert values["construction_site"] is None
    assert values["project_name"] is None
    assert matched and all(matched.values()), matched


def test_generic_fallback_failure_notes_source(tmp_path):
    """兜底解析失败时，错误原因须标注"未命中专属模板"，便于人工复核。"""
    pdf = tmp_path / "broken_invoice.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    page.insert_text((60, 60), "增值税电子普通发票", fontsize=16, fontname="china-s")  # 只有标题
    doc.save(pdf)
    doc.close()

    service, factory = _service()
    with factory() as session:
        doc = service.process_document(session, str(pdf))

    assert doc.template_id == "invoice_generic"
    assert doc.status == "failed"
    assert "未识别到专属模板" in (doc.error_reason or "")


def test_unknown_non_invoice_still_raises(tmp_path):
    """无任何适用模板（含兜底）时，保持原有报错行为。"""
    pdf = tmp_path / "contract.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    page.insert_text((80, 60), "销售合同", fontsize=20, fontname="china-s")
    doc.save(pdf)
    doc.close()

    service, factory = _service()
    with factory() as session:
        with pytest.raises(ValueError, match="未识别到可用模板"):
            service.process_document(session, str(pdf))


# ------------------------------------------------- 解析器：候选锚点 / optional

def test_multi_anchor_tries_candidates_in_order():
    """anchor 列表按序尝试：首个锚点未命中时使用后备锚点。"""
    words = [
        _Word(0, 100, 80, 110, "货物或应税劳务"),
        _Word(0, 130, 90, 140, "钢材"),
    ]
    spec = {"anchor": ["项目名称", "货物或应税劳务"], "direction": "below", "max_distance": 50}

    result = extract_anchor_field("item_name", words, spec)

    assert result.valid, result.errors
    assert result.normalized_value == "钢材"


def test_multi_anchor_all_missing_without_optional_fails():
    """候选锚点全部未命中且非 optional → 失败并提示候选锚点。"""
    spec = {"anchor": ["项目名称", "货物或应税劳务"], "direction": "below"}

    result = extract_anchor_field("item_name", [], spec)

    assert not result.valid
    assert "候选锚点" in result.errors[-1]


def test_optional_missing_is_not_failure():
    """optional 字段未提取到值 → valid 但值为空。"""
    spec = {"anchor": "建筑服务发生地", "direction": "below", "optional": True}

    result = extract_anchor_field("construction_site", [], spec)

    assert result.valid
    assert result.normalized_value is None
    assert result.errors == []
