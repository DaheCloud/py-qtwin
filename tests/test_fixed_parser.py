"""FixedRegionParser 单元测试：用生成的样例 PDF 逐项验证模板引擎与固定区域解析。

运行：.venv/Scripts/python.exe -m pytest tests/test_fixed_parser.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pdf.template_engine import TemplateEngine
from pdf.validators import FieldResult, validate_field

PAGE_W, PAGE_H = 595, 842


# --------------------------------------------------------------- fixtures

@pytest.fixture
def sample_pdf(tmp_path):
    """生成符合模板坐标的合同样例 PDF，返回路径。"""
    path = tmp_path / "sample_contract.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    page.insert_text((80, 60), "销售合同", fontsize=20, fontname="china-s")
    page.insert_text((80, 120), "合同编号：", fontsize=12, fontname="china-s")
    page.insert_text((165, 120), "HT20260901", fontsize=12)  # 值落在 rect (165,100,320,130) 内
    page.insert_text((80, 170), "客户名称：", fontsize=12, fontname="china-s")
    page.insert_text((165, 170), "ABC有限公司", fontsize=12, fontname="china-s")
    page.insert_text((400, 320), "￥12,800.00", fontsize=12, fontname="china-s")  # 带货币符号与千分位，验证清洗（￥ 为全角字符，需 CJK 字体才能正确编码）
    page.insert_text((400, 370), "2026年9月1日", fontsize=12, fontname="china-s")
    doc.save(path)
    doc.close()
    return str(path)


@pytest.fixture
def parser():
    from pdf.pymupdf_parser import FixedRegionParser

    return FixedRegionParser()


# ----------------------------------------------------------- 模板引擎

class TestTemplateEngine:
    def test_loads_all_templates(self, contract_template_dir):
        engine = TemplateEngine(contract_template_dir)
        assert "contract_v1" in engine.all

    def test_detect_by_keyword(self, sample_pdf, contract_template_dir):
        engine = TemplateEngine(contract_template_dir)
        tpl = engine.detect(sample_pdf)
        assert tpl is not None and tpl["template"] == "contract_v1"

    def test_detect_returns_none_for_unknown(self, tmp_path, contract_template_dir):
        path = tmp_path / "other.pdf"
        doc = pymupdf.open()
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_text((72, 72), "totally unrelated", fontsize=14)
        doc.save(path)
        doc.close()

        engine = TemplateEngine(contract_template_dir)
        assert engine.detect(str(path)) is None

    def test_get_unknown_template(self, contract_template_dir):
        engine = TemplateEngine(contract_template_dir)
        assert engine.get("no_such_template") is None

    def test_missing_dir_returns_empty(self, tmp_path):
        engine = TemplateEngine(tmp_path / "does_not_exist")
        assert engine.all == {}


# ------------------------------------------------------- 固定区域解析

class TestFixedRegionParser:
    def test_parse_all_fields(self, sample_pdf, parser, contract_template):
        report = parser.parse(sample_pdf, contract_template)
        assert report.mode == "fixed"
        assert report.normalized == {
            "contract_no": "HT20260901",
            "customer_name": "ABC有限公司",
            "amount": "12800.00",      # ￥ 与千分位已被清洗
            "sign_date": "2026-09-01",  # 中文日期已标准化
        }

    def test_mode_default_is_fixed(self, sample_pdf, parser):
        tpl = {"template": "t", "fields": {}}  # 不写 mode
        report = parser.parse(sample_pdf, tpl)
        assert report.mode == "fixed"

    def test_rejects_dynamic_mode(self, sample_pdf, parser):
        with pytest.raises(ValueError, match="fixed"):
            parser.parse(sample_pdf, {"template": "t", "mode": "dynamic", "fields": {}})

    def test_page_out_of_range(self, sample_pdf, parser):
        tpl = {
            "template": "t",
            "fields": {
                "ghost": {"page": 5, "rect": [80, 100, 250, 130], "type": "string"},
            },
        }
        report = parser.parse(sample_pdf, tpl)
        assert not report.valid
        assert any("超出" in e for e in report.fields["ghost"].errors)

    def test_empty_region_marks_invalid(self, sample_pdf, parser):
        tpl = {
            "template": "t",
            "fields": {
                "empty": {"page": 0, "rect": [500, 700, 590, 740], "type": "string"},
            },
        }
        report = parser.parse(sample_pdf, tpl)
        assert not report.fields["empty"].valid
        assert report.fields["empty"].normalized_value is None

    def test_raw_value_preserved(self, sample_pdf, parser):
        tpl = {
            "template": "t",
            "fields": {
                "amount": {"page": 0, "rect": [400, 300, 550, 330], "type": "decimal"},
            },
        }
        report = parser.parse(sample_pdf, tpl)
        assert report.fields["amount"].raw_value == "￥12,800.00"
        assert report.fields["amount"].normalized_value == "12800.00"


# ------------------------------------------------------------ 字段校验

class TestValidateField:
    def test_pattern_ok(self):
        result = FieldResult("no", "HT20260901", "HT20260901")
        assert validate_field(result, {"type": "string", "pattern": r"^HT\d{8}$"}).valid

    def test_pattern_fail(self):
        result = FieldResult("no", "ABC123", "ABC123")
        checked = validate_field(result, {"type": "string", "pattern": r"^HT\d{8}$"})
        assert not checked.valid
        assert any("正则" in e for e in checked.errors)

    def test_empty_value_fails(self):
        result = FieldResult("no", "", None)
        assert not validate_field(result, {"type": "string"}).valid
