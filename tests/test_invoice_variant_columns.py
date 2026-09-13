"""版式变体回归：明细表头含"规格型号/单位"列、后续列整体右移的发票。

背景：第二版式与 invoice_v1 版式的差异有两处——明细表头多了"规格型号/单位"
两列（金额/税额列整体右移、表头文字带字间距），且没有建筑服务信息块。
两个专属模板的分工：
  - 带建筑服务信息块（建筑服务发生地/建筑项目名称）→ invoice_v1（必填校验）
  - 无信息块的其余发票 → invoice_v2（信息块两项可选，缺失不报错）

运行：.venv/Scripts/python.exe -m pytest tests/test_invoice_variant_columns.py -v
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.db import get_engine, init_db, make_session_factory
from pdf.template_engine import TemplateEngine
from services.pdf_service import PdfService

PAGE_W, PAGE_H = 595.32, 841.92
TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"


def _make_variant_invoice(
    path: Path, *, with_construction_block: bool = True, rows: int = 1
) -> None:
    """仿真第二版式：明细表头为 项目名称/规格型号/单位/数量/单价/金额/税率/税额。

    rows 控制明细行数，用于验证多行明细的"仅提取首行 + 行数标注"行为。
    """
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)

    page.insert_text((60, 60), "电子发票（增值税专用发票）", fontsize=16, fontname="china-s")
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
    page.insert_text((165, header_y), "规格型号", fontsize=9, fontname="china-s")
    page.insert_text((222, header_y), "单 位", fontsize=9, fontname="china-s")
    page.insert_text((265, header_y), "数 量", fontsize=9, fontname="china-s")
    page.insert_text((310, header_y), "单 价", fontsize=9, fontname="china-s")
    page.insert_text((365, header_y), "金 额", fontsize=9, fontname="china-s")
    page.insert_text((425, header_y), "税率/征收率", fontsize=9, fontname="china-s")
    page.insert_text((510, header_y), "税 额", fontsize=9, fontname="china-s")

    row_y = header_y + 22
    for _ in range(rows):
        page.insert_text((60, row_y), "*建筑服务*劳务工程款", fontsize=9, fontname="china-s")
        page.insert_text((165, row_y), "—", fontsize=9, fontname="china-s")
        page.insert_text((222, row_y), "项", fontsize=9, fontname="china-s")
        page.insert_text((265, row_y), "1", fontsize=9)
        page.insert_text((310, row_y), "73933.20", fontsize=9)
        page.insert_text((365, row_y), "73933.20", fontsize=9)
        page.insert_text((425, row_y), "3%", fontsize=9)
        page.insert_text((510, row_y), "2218.00", fontsize=9)
        row_y += 22

    # 建筑服务信息（明细区下方，标签与值分行）；第二版式没有该信息块
    if with_construction_block:
        page.insert_text((60, row_y + 20), "建筑服务发生地：", fontsize=9, fontname="china-s")
        page.insert_text((60, row_y + 32), "福建省厦门市湖里区仙岳医院院区", fontsize=9, fontname="china-s")
        page.insert_text((60, row_y + 66), "建筑项目名称：", fontsize=9, fontname="china-s")
        page.insert_text((60, row_y + 78), "厦门市仙岳医院改扩建项目地下室及上部主体工程", fontsize=9, fontname="china-s")

    total_y = row_y + 120
    # 合计 = 各行之和（发票数据自洽，业务数学校验才有意义）
    total_amount = Decimal("73933.20") * rows
    total_tax = Decimal("2218.00") * rows
    page.insert_text((60, total_y), "合 计", fontsize=9, fontname="china-s")
    page.insert_text((365, total_y), f"¥{total_amount:.2f}", fontsize=9, fontname="china-s")
    page.insert_text((510, total_y), f"¥{total_tax:.2f}", fontsize=9, fontname="china-s")
    page.insert_text(
        (360, total_y + 30), f"（小写）¥{total_amount + total_tax:.2f}", fontsize=9, fontname="china-s"
    )

    doc.save(path)
    doc.close()


def test_variant_columns_parse_with_invoice_v1(tmp_path):
    """变体版式命中 invoice_v1，全字段解析成功且交叉验证一致。"""
    pdf = tmp_path / "variant_invoice.pdf"
    _make_variant_invoice(pdf)

    engine = TemplateEngine(TEMPLATES_DIR)
    tpl, kind = engine.resolve(str(pdf))
    assert kind == "match" and tpl is not None and tpl["template"] == "invoice_v1"

    db_engine = get_engine(":memory:")
    init_db(db_engine)
    service = PdfService(engine)
    with make_session_factory(db_engine)() as session:
        doc = service.process_document(session, str(pdf), cross_verify=True)
        values = {f.field_name: f.normalized_value for f in doc.fields}
        matched = {v.field_name: v.matched for v in doc.verifications}
        status, reason = doc.status, doc.error_reason

    assert status == "success", reason
    assert values["invoice_no"] == "26942000000871475416"
    assert values["invoice_date"] == "2026-09-09"
    assert values["buyer_name"] == "中磐建设集团有限公司"
    assert values["seller_name"] == "厦门仪翔建设工程有限公司"
    # 回归点：表头带字间距（"金 额"/"税 额"）且列位右移时仍取到合计行的值
    assert values["item_name"] == "*建筑服务*劳务工程款"
    assert values["tax_rate"] == "3%"
    assert values["amount"] == "73933.20"
    assert values["tax_amount"] == "2218.00"
    assert values["total_amount"] == "76151.20"
    assert values["construction_site"] == "福建省厦门市湖里区仙岳医院院区"
    assert values["project_name"] == "厦门市仙岳医院改扩建项目地下室及上部主体工程"
    # 明细辅助列：带字间距表头（"单 位/数 量/单 价"）靠相邻词拼接命中
    assert values["unit"] == "项"
    assert values["quantity"] == "1"
    assert values["unit_price"] == "73933.20"
    assert values["item_rows"] == "1"
    assert matched and all(matched.values()), matched


def test_variant_columns_without_block_uses_invoice_v2(tmp_path):
    """第二版式（无建筑服务信息块）：识别到 invoice_v2，两项可选字段留空不报错。"""
    pdf = tmp_path / "variant_invoice_no_block.pdf"
    _make_variant_invoice(pdf, with_construction_block=False)

    engine = TemplateEngine(TEMPLATES_DIR)
    tpl, kind = engine.resolve(str(pdf))
    assert kind == "match" and tpl is not None and tpl["template"] == "invoice_v2"

    db_engine = get_engine(":memory:")
    init_db(db_engine)
    service = PdfService(engine)
    with make_session_factory(db_engine)() as session:
        doc = service.process_document(session, str(pdf), cross_verify=True)
        values = {f.field_name: f.normalized_value for f in doc.fields}
        matched = {v.field_name: v.matched for v in doc.verifications}
        status, reason = doc.status, doc.error_reason

    assert status == "success", reason
    assert values["invoice_no"] == "26942000000871475416"
    assert values["invoice_date"] == "2026-09-09"
    assert values["buyer_name"] == "中磐建设集团有限公司"
    assert values["seller_name"] == "厦门仪翔建设工程有限公司"
    assert values["item_name"] == "*建筑服务*劳务工程款"
    assert values["tax_rate"] == "3%"
    assert values["amount"] == "73933.20"
    assert values["tax_amount"] == "2218.00"
    assert values["total_amount"] == "76151.20"
    # 信息块缺失：可选字段留空，不影响整单成功
    assert values["construction_site"] is None
    assert values["project_name"] is None
    # 规格型号列是"—"占位符 → 清洗后视为空；单行明细不触发多行标注
    assert values["spec_model"] is None
    assert values["unit"] == "项"
    assert values["quantity"] == "1"
    assert values["unit_price"] == "73933.20"
    assert values["item_rows"] == "1"
    assert matched and all(matched.values()), matched


def test_variant_multi_row_items_fully_extracted(tmp_path):
    """多行明细（2 行）由表格引擎逐行重建（方案 §P0）：整单 success、无告警、items 全落库。

    多行商品明细是正常的发票结构，不再因为行数 > 1 就转人工复核，也不再提示
    "仅提取首行"——逐行 items 已建立行关联；只有结构异常（缺列、行数据不完整等）
    才转人工（见结构校验用例）。
    """
    pdf = tmp_path / "variant_invoice_2rows.pdf"
    _make_variant_invoice(pdf, with_construction_block=False, rows=2)

    engine = TemplateEngine(TEMPLATES_DIR)
    tpl, kind = engine.resolve(str(pdf))
    assert kind == "match" and tpl is not None and tpl["template"] == "invoice_v2"

    db_engine = get_engine(":memory:")
    init_db(db_engine)
    service = PdfService(engine)
    with make_session_factory(db_engine)() as session:
        doc = service.process_document(session, str(pdf), cross_verify=True)
        values = {f.field_name: f.normalized_value for f in doc.fields}
        matched = {v.field_name: v.matched for v in doc.verifications}
        items = [(it.row_index, it.name, it.unit, it.amount, it.tax) for it in doc.items]
        status, reason = doc.status, doc.error_reason

    assert status == "success", reason
    assert reason is None
    # 结构校验通过：明细 2 行，金额/税额列各 2 行（由 items 统计）
    assert values["item_rows"] == "2"
    assert values["item_amount_rows"] == "2"
    assert values["item_tax_rows"] == "2"
    # 逐行 items：行关联已建立
    assert len(items) == 2
    assert items[0] == (1, "*建筑服务*劳务工程款", "项", "73933.20", "2218.00")
    assert items[1] == (2, "*建筑服务*劳务工程款", "项", "73933.20", "2218.00")
    # 首行明细字段
    assert values["item_name"] == "*建筑服务*劳务工程款"
    assert values["unit"] == "项"
    assert values["quantity"] == "1"
    assert values["unit_price"] == "73933.20"
    # 合计字段仍取合计行（合计 = 2 行之和）
    assert values["amount"] == "147866.40"
    assert values["tax_amount"] == "4436.00"
    assert values["total_amount"] == "152302.40"
    assert matched and all(matched.values()), matched
