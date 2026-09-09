"""动态区域解析器单元测试 + 布局漂移兜底集成测试。

运行：.venv/Scripts/python.exe -m pytest tests/test_dynamic_parser.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.db import get_engine, init_db, make_session_factory
from pdf.template_engine import TemplateEngine
from services.pdf_service import PdfService

PAGE_W, PAGE_H = 595, 842


def _make_contract(path: Path, *, shift_y: float = 0.0, shift_x: float = 0.0) -> None:
    """生成合同 PDF；shift 用于模拟版式漂移（固定 rect 会落空）。"""
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    page.insert_text((80 + shift_x, 60 + shift_y), "销售合同", fontsize=20, fontname="china-s")
    # 标签与值同行分开书写：固定解析读值矩形，动态解析用锚点找右侧值
    page.insert_text(
        (80 + shift_x, 120 + shift_y), "合同编号：",
        fontsize=12, fontname="china-s",
    )
    page.insert_text((165 + shift_x, 120 + shift_y), "HT20260901", fontsize=12)
    page.insert_text(
        (80 + shift_x, 170 + shift_y), "客户名称：",
        fontsize=12, fontname="china-s",
    )
    page.insert_text((165 + shift_x, 170 + shift_y), "ABC有限公司", fontsize=12, fontname="china-s")
    page.insert_text((400 + shift_x, 320 + shift_y), "12800.00", fontsize=12)
    page.insert_text((400 + shift_x, 370 + shift_y), "2026-09-01", fontsize=12)
    doc.save(path)
    doc.close()


@pytest.fixture
def normal_pdf(tmp_path):
    path = tmp_path / f"contract_normal_{abs(hash(tmp_path)) % 99999}.pdf"
    _make_contract(path)
    return str(path)


@pytest.fixture
def shifted_pdf(tmp_path):
    """整体下移 80pt、右移 60pt —— 固定 rect 全部落空的布局漂移场景。"""
    path = tmp_path / f"contract_shifted_{abs(hash(tmp_path)) % 99999}.pdf"
    _make_contract(path, shift_y=80, shift_x=60)
    return str(path)


@pytest.fixture
def dynamic_template():
    tpl = TemplateEngine("templates").get("contract_v1")
    return {**tpl, "mode": "dynamic"}


@pytest.fixture
def parser():
    from pdf.dynamic_parser import DynamicRegionParser

    return DynamicRegionParser()


# ------------------------------------------------------- 动态解析单项

class TestDynamicRegionParser:
    def test_only_accepts_dynamic_mode(self, normal_pdf, parser):
        with pytest.raises(ValueError, match="dynamic"):
            parser.parse(normal_pdf, {"template": "t", "mode": "fixed", "fields": {}})

    def test_anchor_same_word_value(self, normal_pdf, parser, dynamic_template):
        """'合同编号：HT20260901' 同词情形，冒号后被正确剥离。"""
        report = parser.parse(normal_pdf, dynamic_template)
        fr = report.fields["contract_no"]
        assert fr.valid, fr.errors
        assert fr.raw_value == "HT20260901"
        assert fr.normalized_value == "HT20260901"
        assert fr.parser == "pymupdf-dynamic"

    def test_customer_name_right(self, normal_pdf, parser, dynamic_template):
        report = parser.parse(normal_pdf, dynamic_template)
        fr = report.fields["customer_name"]
        assert fr.valid, fr.errors
        assert fr.normalized_value == "ABC有限公司"

    def test_plain_value_fields_still_parse(self, normal_pdf, parser, dynamic_template):
        """无锚点的字段（amount/sign_date）在动态模板下：无 anchor 应标记失败而不是崩溃。"""
        report = parser.parse(normal_pdf, dynamic_template)
        assert not report.fields["amount"].valid
        assert "anchor" in "".join(report.fields["amount"].errors)

    def test_missing_anchor_fails_gracefully(self, tmp_path, parser):
        path = tmp_path / "blank.pdf"
        doc = pymupdf.open()
        doc.new_page(width=PAGE_W, height=PAGE_H)
        doc.save(path)
        doc.close()

        report = parser.parse(str(path), {
            "template": "t",
            "mode": "dynamic",
            "fields": {
                "no": {"page": 0, "anchor": "合同编号", "direction": "right", "pattern": r"^HT\d{8}$"},
            },
        })
        assert not report.fields["no"].valid
        assert any("锚点" in e for e in report.fields["no"].errors)

    def test_pattern_filters_wrong_candidate(self, tmp_path, parser):
        """锚点右侧的词不匹配 pattern 时应继续找，找不到则失败。"""
        path = tmp_path / "wrong.pdf"
        doc = pymupdf.open()
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_text((80, 120), "合同编号：BAD123", fontsize=12, fontname="china-s")
        doc.save(path)
        doc.close()

        report = parser.parse(str(path), {
            "template": "t",
            "mode": "dynamic",
            "fields": {
                "no": {"page": 0, "anchor": "合同编号", "direction": "right", "pattern": r"^HT\d{8}$"},
            },
        })
        assert not report.fields["no"].valid

    def test_below_direction(self, tmp_path, parser):
        path = tmp_path / "below.pdf"
        doc = pymupdf.open()
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_text((80, 120), "客户名称", fontsize=12, fontname="china-s")
        page.insert_text((80, 160), "XYZ公司", fontsize=12, fontname="china-s")  # 下方 40pt
        doc.save(path)
        doc.close()

        report = parser.parse(str(path), {
            "template": "t",
            "mode": "dynamic",
            "fields": {
                "name": {"page": 0, "anchor": "客户名称", "direction": "below", "max_distance": 50},
            },
        })
        assert report.fields["name"].valid
        assert report.fields["name"].normalized_value == "XYZ公司"

    def test_below_respects_max_distance(self, tmp_path, parser):
        path = tmp_path / "far.pdf"
        doc = pymupdf.open()
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_text((80, 120), "客户名称", fontsize=12, fontname="china-s")
        page.insert_text((80, 220), "XYZ公司", fontsize=12, fontname="china-s")  # 下方 100pt > 50
        doc.save(path)
        doc.close()

        report = parser.parse(str(path), {
            "template": "t",
            "mode": "dynamic",
            "fields": {
                "name": {"page": 0, "anchor": "客户名称", "direction": "below", "max_distance": 50},
            },
        })
        assert not report.fields["name"].valid

    def test_shifted_layout_still_found(self, shifted_pdf, parser, dynamic_template):
        """版式整体漂移后，锚点策略依然命中。"""
        report = parser.parse(shifted_pdf, dynamic_template)
        assert report.fields["contract_no"].valid
        assert report.fields["contract_no"].normalized_value == "HT20260901"
        assert report.fields["customer_name"].valid


# ------------------------------------------------------- 兜底流程集成

class TestFallbackFlow:
    def _service(self):
        return PdfService(TemplateEngine("templates"))

    def test_fixed_success_no_fallback(self, normal_pdf):
        """固定解析全部成功 → 不触发兜底。"""
        engine = get_engine(":memory:")
        init_db(engine)
        service = self._service()
        with make_session_factory(engine)() as session:
            doc = service.process_document(session, normal_pdf)
            assert doc.status == "success"
            assert all(f.parser == "pymupdf" for f in doc.fields)

    def test_fixed_fails_dynamic_rescues(self, shifted_pdf):
        """布局漂移：固定 rect 落空失败 → 锚点兜底救回 → manual_review/成功。"""
        engine = get_engine(":memory:")
        init_db(engine)
        service = self._service()
        with make_session_factory(engine)() as session:
            doc = service.process_document(session, shifted_pdf)
            parsers = {f.field_name: f.parser for f in doc.fields}
            # 有锚点规则的字段被动态解析救回
            assert parsers["contract_no"] == "pymupdf-dynamic"
            assert parsers["customer_name"] == "pymupdf-dynamic"
            # 无锚点规则的字段仍是固定解析的失败结果
            assert parsers["amount"] == "pymupdf"
            assert doc.status in ("manual_review", "failed")

    def test_dynamic_mode_template_direct(self, tmp_path, shifted_pdf):
        """mode=dynamic 的模板直接走动态解析，不经过固定。"""
        engine = get_engine(":memory:")
        init_db(engine)
        service = self._service()
        tpl = {**TemplateEngine("templates").get("contract_v1"), "mode": "dynamic"}
        # 去掉无锚点字段，纯动态模板
        tpl["fields"] = {
            k: v for k, v in tpl["fields"].items() if "anchor" in v
        }
        tpl["business_rules"] = []
        with make_session_factory(engine)() as session:
            doc = service.process_document(session, shifted_pdf, tpl)
            assert doc.status == "success"
            assert all(f.parser == "pymupdf-dynamic" for f in doc.fields)
