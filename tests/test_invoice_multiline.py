"""多行明细发票回归测试：明细行数变化时，合计字段必须稳定命中。

背景（修复的缺陷）：
  1. amount/tax_amount 原先用"表头锚点 + 固定 max_distance=110pt"定位合计行，
     明细行数增多会把合计行推出距离范围 → 字段解析失败 → 整单 failed；
  2. "税"字锚点同时命中"纳税人识别号/税率/税额"等多处，候选按锚点遍历顺序
     取首个匹配，会借用相邻列（金额列）的候选 → tax_amount 取成金额值。

修复后行为：
  - 候选按"与锚点的列重叠质量"分层，严格同列优先、擦边候选兜底；
  - 模板 amount/tax_amount 用 max_distance=800 + pick=last 取该列最下方的合计值。

运行：.venv/Scripts/python.exe -m pytest tests/test_invoice_multiline.py -v
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

PAGE_W, PAGE_H = 595.32, 841.92


def _make_invoice(path: Path, rows: int) -> None:
    """生成仿真数电票（建筑服务版式）：rows 行商品明细，合计行随行数下移。"""
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)

    page.insert_text((60, 60), "电子发票（普通发票）", fontsize=16, fontname="china-s")
    page.insert_text((330, 90), "发票号码：", fontsize=10, fontname="china-s")
    page.insert_text((395, 90), "26942000000871475416", fontsize=10)
    page.insert_text((330, 110), "开票日期：", fontsize=10, fontname="china-s")
    page.insert_text((395, 110), "2026年09月09日", fontsize=10, fontname="china-s")

    page.insert_text((60, 160), "名称：", fontsize=10, fontname="china-s")
    page.insert_text((95, 160), "中磐建设集团有限公司", fontsize=10, fontname="china-s")
    page.insert_text((60, 178), "纳税人识别号：", fontsize=10, fontname="china-s")
    page.insert_text((135, 178), "914114005698015827", fontsize=10)

    page.insert_text((330, 160), "名称：", fontsize=10, fontname="china-s")
    page.insert_text((365, 160), "厦门仪翔建设工程有限公司", fontsize=10, fontname="china-s")
    page.insert_text((330, 178), "纳税人识别号：", fontsize=10, fontname="china-s")
    page.insert_text((405, 178), "913502000658790529", fontsize=10)

    header_y = 260
    page.insert_text((60, header_y), "项目名称", fontsize=9, fontname="china-s")
    page.insert_text((230, header_y), "数量", fontsize=9, fontname="china-s")
    page.insert_text((290, header_y), "单价", fontsize=9, fontname="china-s")
    page.insert_text((350, header_y), "金额", fontsize=9, fontname="china-s")
    page.insert_text((420, header_y), "税率/征收率", fontsize=9, fontname="china-s")
    page.insert_text((500, header_y), "税额", fontsize=9, fontname="china-s")

    y = header_y + 22
    for _ in range(rows):
        page.insert_text((60, y), "*建筑服务*劳务工程款", fontsize=9, fontname="china-s")
        page.insert_text((230, y), "1", fontsize=9)
        page.insert_text((290, y), "73933.20", fontsize=9)
        page.insert_text((350, y), "73933.20", fontsize=9)
        page.insert_text((420, y), "3%", fontsize=9)
        page.insert_text((500, y), "2218.00", fontsize=9)
        y += 22

    # 建筑服务信息（明细区下方，标签与值分行）
    page.insert_text((60, y + 20), "建筑服务发生地：", fontsize=9, fontname="china-s")
    page.insert_text((60, y + 32), "福建省厦门市湖里区仙岳医院院区", fontsize=9, fontname="china-s")
    page.insert_text((60, y + 66), "建筑项目名称：", fontsize=9, fontname="china-s")
    page.insert_text((60, y + 78), "厦门市仙岳医院改扩建项目地下室及上部主体工程", fontsize=9, fontname="china-s")

    # 合计行 / 价税合计
    total_y = y + 110
    page.insert_text((60, total_y), "合 计", fontsize=9, fontname="china-s")
    page.insert_text((350, total_y), "¥73933.20", fontsize=9, fontname="china-s")
    page.insert_text((500, total_y), "¥2218.00", fontsize=9, fontname="china-s")
    page.insert_text((360, total_y + 30), "（小写）¥76151.20", fontsize=9, fontname="china-s")

    doc.save(path)
    doc.close()


@pytest.fixture
def invoice_engine() -> TemplateEngine:
    """生产模板目录（templates/invoice_v1.json）。"""
    return TemplateEngine(Path(__file__).resolve().parents[1] / "templates")


def _process(pdf_path: Path, engine: TemplateEngine):
    """跑完整管线，返回 (doc, {字段: 归一化值}, {字段: 交叉验证是否一致})。"""
    db_engine = get_engine(":memory:")
    init_db(db_engine)
    factory = make_session_factory(db_engine)
    service = PdfService(engine)
    with factory() as session:
        doc = service.process_document(session, str(pdf_path))
        values = {f.field_name: f.normalized_value for f in doc.fields}
        matched = {v.field_name: v.matched for v in doc.verifications}
        return doc, values, matched


# ------------------------------------------------- 端到端：多行明细

@pytest.mark.parametrize("rows", [1, 3, 5, 12])
def test_multiline_invoice_parses_with_accumulated_totals(tmp_path, invoice_engine, rows):
    """任意明细行数下（合计行随之下移）应解析成功，合计字段取合计行而非明细行。"""
    pdf = tmp_path / f"invoice_{rows}rows.pdf"
    _make_invoice(pdf, rows)

    doc, values, matched = _process(pdf, invoice_engine)

    assert doc.status == "success", doc.error_reason
    assert values["amount"] == "73933.20"
    # 回归点：修复前 tax_amount 会借用金额列的候选取成 73933.20
    assert values["tax_amount"] == "2218.00"
    assert values["total_amount"] == "76151.20"
    assert values["item_name"] == "*建筑服务*劳务工程款"
    assert values["tax_rate"] == "3%"
    assert all(matched.values()), matched


@pytest.mark.parametrize("rows", [5, 12])
def test_multiline_cross_verification_agrees(tmp_path, invoice_engine, rows):
    """明细多行时 PyMuPDF 与 pdfplumber 两侧结果仍然一致。"""
    pdf = tmp_path / f"invoice_cross_{rows}rows.pdf"
    _make_invoice(pdf, rows)

    doc, values, matched = _process(pdf, invoice_engine)

    assert matched, "关键字段必须产生交叉验证记录"
    assert all(matched.values()), matched


# ------------------------------------------------- 引擎：候选分层与 pick

def test_strict_same_column_priority_over_edge_overlap():
    """擦边重叠的相邻列候选不得抢先命中：严格同列的值优先。"""
    words = [
        _Word(0, 100, 60, 110, "税"),        # 命中"税"，其下只有擦边候选
        _Word(120, 100, 180, 110, "税额"),    # 命中"税"，其下有严格同列值
        _Word(50, 130, 118, 140, "¥111.00"),  # 与"税"擦边重叠 → 宽松层
        _Word(120, 200, 186, 210, "¥222.00"),  # 与"税额"完全同列 → 严格层
    ]
    spec = {
        "anchor": "税", "direction": "below", "max_distance": 200,
        "below_mode": "each", "type": "decimal",
        "pattern": r"^[¥￥][\d,]+\.\d{2}$",
    }
    result = extract_anchor_field("tax_amount", words, spec)
    assert result.normalized_value == "222.00", result.errors


def test_pick_last_takes_lowest_value_in_column():
    """pick=last：同列多个匹配时取最下方的那个（合计行在明细行下方）。"""
    words = [
        _Word(100, 100, 130, 110, "金额"),
        _Word(100, 130, 170, 140, "¥111.00"),
        _Word(100, 400, 170, 410, "¥222.00"),  # 合计行（更靠下，在 max_distance 内）
    ]
    spec = {
        "anchor": "金额", "direction": "below", "max_distance": 800,
        "below_mode": "each", "pick": "last", "type": "decimal",
        "pattern": r"^[¥￥][\d,]+\.\d{2}$",
    }
    result = extract_anchor_field("amount", words, spec)
    assert result.normalized_value == "222.00", result.errors


def test_pick_first_default_unchanged():
    """默认 pick=first 保持原语义：取首个匹配。"""
    words = [
        _Word(100, 100, 130, 110, "金额"),
        _Word(100, 130, 170, 140, "¥111.00"),
        _Word(100, 400, 170, 410, "¥222.00"),
    ]
    spec = {
        "anchor": "金额", "direction": "below", "max_distance": 800,
        "below_mode": "each", "type": "decimal",
        "pattern": r"^[¥￥][\d,]+\.\d{2}$",
    }
    result = extract_anchor_field("amount", words, spec)
    assert result.normalized_value == "111.00", result.errors
