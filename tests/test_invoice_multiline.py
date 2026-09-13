"""多行明细发票回归测试：明细行数变化时，合计字段必须稳定命中。

背景（修复的缺陷）：
  1. amount/tax_amount 原先用"表头锚点 + 固定 max_distance=110pt"定位合计行，
     明细行数增多会把合计行推出距离范围 → 字段解析失败 → 整单 failed；
  2. "税"字锚点同时命中"纳税人识别号/税率/税额"等多处，候选按锚点遍历顺序
     取首个匹配，会借用相邻列（金额列）的候选 → tax_amount 取成金额值。

修复后行为：
  - 候选按"与锚点的列重叠质量"分层，严格同列优先、擦边候选兜底；
  - 模板 amount/tax_amount 用 max_distance=800 + pick=last 取该列最下方的合计值；
  - 明细逐行由 Table Engine 重建（合计行 = 各行之和，业务数学校验通过）。

运行：.venv/Scripts/python.exe -m pytest tests/test_invoice_multiline.py -v
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest
import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.db import get_engine, init_db, make_session_factory
from pdf.dynamic_parser import _Word, extract_anchor_field
from pdf.template_engine import TemplateEngine
from services.pdf_service import PdfService

PAGE_W, PAGE_H = 595.32, 841.92


def _make_invoice(
    path: Path,
    rows: int,
    *,
    wrap_project_name: bool = False,
    wrap_line_gap: float = 12.0,
    with_currency_symbol: bool = True,
    split_decimal: bool = False,
) -> None:
    """生成仿真数电票（建筑服务版式）：rows 行商品明细，合计行随行数下移。

    wrap_project_name=True 时把建筑项目名称折成三行（模拟长文案单元格），
    wrap_line_gap 控制折行行距，用于验证多行值的合并取值。
    with_currency_symbol=False 模拟金额列不带 ¥ 符号的实票（只在明细/合计
    列写纯数字），用于验证金额字段的取值不依赖货币符号。
    split_decimal=True 模拟实票里"小数点后有空隙"的切词（"118812. 57"），
    用于验证数值多段拼接。
    """

    def money(value: str) -> str:
        if not split_decimal:
            return value
        int_part, _, dec_part = value.partition(".")
        return f"{int_part}. {dec_part}"

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
        page.insert_text((350, y), money("73933.20"), fontsize=9)
        page.insert_text((420, y), "3%", fontsize=9)
        page.insert_text((500, y), money("2218.00"), fontsize=9)
        y += 22

    # 建筑服务信息（明细区下方，标签与值分行）
    page.insert_text((60, y + 20), "建筑服务发生地：", fontsize=9, fontname="china-s")
    page.insert_text((60, y + 32), "福建省厦门市湖里区仙岳医院院区", fontsize=9, fontname="china-s")
    page.insert_text((60, y + 66), "建筑项目名称：", fontsize=9, fontname="china-s")
    project_lines = (
        ("厦门市仙岳医院", "改扩建项目地下室", "及上部主体工程")
        if wrap_project_name
        else ("厦门市仙岳医院改扩建项目地下室及上部主体工程",)
    )
    for offset, part in enumerate(project_lines):
        page.insert_text((60, y + 78 + offset * wrap_line_gap), part, fontsize=9, fontname="china-s")

    # 合计行 / 价税合计（折行后需相应下移，保持与明细区的间距）
    # 合计 = 各行之和（发票数据自洽，业务数学校验才有意义）
    total_amount = Decimal("73933.20") * rows
    total_tax = Decimal("2218.00") * rows
    grand_total = total_amount + total_tax
    last_value_y = y + 78 + (len(project_lines) - 1) * wrap_line_gap
    total_y = max(y + 110, last_value_y + 30)
    symbol = "¥" if with_currency_symbol else ""
    page.insert_text((60, total_y), "合 计", fontsize=9, fontname="china-s")
    page.insert_text(
        (350, total_y), f"{symbol}{money(f'{total_amount:.2f}')}", fontsize=9, fontname="china-s"
    )
    page.insert_text(
        (500, total_y), f"{symbol}{money(f'{total_tax:.2f}')}", fontsize=9, fontname="china-s"
    )
    page.insert_text(
        (360, total_y + 30), f"（小写）{symbol}{grand_total:.2f}", fontsize=9, fontname="china-s"
    )

    doc.save(path)
    doc.close()


@pytest.fixture
def invoice_engine() -> TemplateEngine:
    """生产模板目录（templates/invoice_v1.json）。"""
    return TemplateEngine(Path(__file__).resolve().parents[1] / "templates")


def _process(pdf_path: Path, engine: TemplateEngine, *, cross_verify: bool = True):
    """跑完整管线，返回 (doc, {字段: 归一化值}, {字段: 交叉验证是否一致}, items 明细行)。

    默认 cross_verify=True：本文件重点验证"双引擎取值一致"这一回归保障；
    Lazy 触发策略（默认自动）另有专项测试（test_lazy_cross_validation.py）。
    """
    db_engine = get_engine(":memory:")
    init_db(db_engine)
    factory = make_session_factory(db_engine)
    service = PdfService(engine)
    with factory() as session:
        doc = service.process_document(session, str(pdf_path), cross_verify=cross_verify)
        values = {f.field_name: f.normalized_value for f in doc.fields}
        matched = {v.field_name: v.matched for v in doc.verifications}
        items = [
            (it.row_index, it.name, it.quantity, it.unit_price, it.amount, it.tax_rate, it.tax)
            for it in doc.items
        ]
        return doc, values, matched, items


# ------------------------------------------------- 端到端：多行明细

@pytest.mark.parametrize("rows", [1, 3, 5, 12])
def test_multiline_invoice_parses_with_accumulated_totals(tmp_path, invoice_engine, rows):
    """任意明细行数下（合计行随之下移）字段仍取对位置：合计字段取合计行而非明细行。

    多行明细属**正常发票结构**（方案 §7）：Table Engine 逐行重建 items，
    全部行都提取，不再有"仅提取首行"告警，整单保持 success。
    """
    pdf = tmp_path / f"invoice_{rows}rows.pdf"
    _make_invoice(pdf, rows)

    doc, values, matched, items = _process(pdf, invoice_engine)

    assert doc.status == "success", doc.error_reason
    assert doc.error_reason is None
    # 明细行数（标注用，随导出带出）与逐行 items 一致
    assert values["item_rows"] == str(rows)
    assert len(items) == rows
    assert [item[0] for item in items] == list(range(1, rows + 1))
    # 明细首行字段
    assert values["item_name"] == "*建筑服务*劳务工程款"
    assert values["quantity"] == "1"
    assert values["unit_price"] == "73933.20"
    # 该版式没有规格型号/单位列 → 可选字段留空
    assert values["spec_model"] is None
    assert values["unit"] is None
    # 合计字段仍取合计行（合计 = 各行之和：逐行 items 的业务数学校验据此通过）
    assert Decimal(values["amount"]) == Decimal("73933.20") * rows
    # 回归点：修复前 tax_amount 会借用金额列的候选取成 73933.20
    assert Decimal(values["tax_amount"]) == Decimal("2218.00") * rows
    assert Decimal(values["total_amount"]) == Decimal("76151.20") * rows
    assert values["tax_rate"] == "3%"
    assert all(matched.values()), matched


@pytest.mark.parametrize("rows", [5, 12])
def test_multiline_cross_verification_agrees(tmp_path, invoice_engine, rows):
    """明细多行时 PyMuPDF 与 pdfplumber 两侧结果仍然一致。"""
    pdf = tmp_path / f"invoice_cross_{rows}rows.pdf"
    _make_invoice(pdf, rows)

    doc, values, matched, items = _process(pdf, invoice_engine)

    assert matched, "关键字段必须产生交叉验证记录"
    assert all(matched.values()), matched


# ------------------------------------------------- 金额列不带货币符号

def test_amounts_without_currency_symbol_still_parsed(tmp_path, invoice_engine):
    """金额列不带 ¥ 符号（实票常见）时，金额/税额/价税合计仍应取到合计行的值。"""
    pdf = tmp_path / "invoice_no_symbol.pdf"
    _make_invoice(pdf, rows=1, with_currency_symbol=False)

    doc, values, matched, items = _process(pdf, invoice_engine)

    assert values["amount"] == "73933.20"
    assert values["tax_amount"] == "2218.00"
    assert values["total_amount"] == "76151.20"
    assert doc.status == "success", doc.error_reason
    assert all(matched.values()), matched


# ------------------------------------------------- 金额被切词成多段

def test_amount_split_by_gap_still_parsed(tmp_path, invoice_engine):
    """金额小数点后有间隙被切成多段（"73933." + "20"）时仍应取到合计行的值。"""
    pdf = tmp_path / "invoice_split_decimal.pdf"
    _make_invoice(pdf, rows=1, split_decimal=True)

    doc, values, matched, items = _process(pdf, invoice_engine)

    assert values["amount"] == "73933.20"
    assert values["tax_amount"] == "2218.00"
    assert doc.status == "success", doc.error_reason
    assert all(matched.values()), matched


# ------------------------------------------------- 建筑服务字段：长文案折行

@pytest.mark.parametrize("line_gap", [12.0, 16.0, 20.0])
def test_wrapped_project_name_merged_completely(tmp_path, invoice_engine, line_gap):
    """建筑项目名称文案过长折成三行时，三行必须合并为完整值：
    既不能截断（旧实现 max_distance=30 会丢掉靠下的行），
    也不能把后面的"合 计"行并进来。
    """
    pdf = tmp_path / f"invoice_wrapped_{int(line_gap)}.pdf"
    _make_invoice(pdf, rows=1, wrap_project_name=True, wrap_line_gap=line_gap)

    doc, values, matched, items = _process(pdf, invoice_engine)

    assert values["project_name"] == "厦门市仙岳医院改扩建项目地下室及上部主体工程"
    assert "合计" not in (values["project_name"] or "")
    assert values["construction_site"] == "福建省厦门市湖里区仙岳医院院区"
    assert doc.status == "success", doc.error_reason
    assert all(matched.values()), matched


# ------------------------------------------------- 跨页续表（合计行在第 2 页）


def _make_cross_page_invoice(path: Path) -> None:
    """跨页续表发票：第 1 页只有头部 + 表头 + 2 行明细（无合计行）；
    第 2 页续 1 行明细 + 合计行 + 价税合计。
    合计 = 3 行之和：73933.20 + 1200.00 + 300.00 = 75433.20（税 2263.00）。
    """
    doc = pymupdf.open()
    page1 = doc.new_page(width=PAGE_W, height=PAGE_H)
    page1.insert_text((60, 60), "电子发票（普通发票）", fontsize=16, fontname="china-s")
    page1.insert_text((330, 90), "发票号码：", fontsize=10, fontname="china-s")
    page1.insert_text((395, 90), "26942000000871475416", fontsize=10)
    page1.insert_text((330, 110), "开票日期：", fontsize=10, fontname="china-s")
    page1.insert_text((395, 110), "2026年09月09日", fontsize=10, fontname="china-s")
    page1.insert_text((60, 160), "名称：", fontsize=10, fontname="china-s")
    page1.insert_text((95, 160), "中磐建设集团有限公司", fontsize=10, fontname="china-s")
    page1.insert_text((60, 178), "纳税人识别号：", fontsize=10, fontname="china-s")
    page1.insert_text((135, 178), "914114005698015827", fontsize=10)
    page1.insert_text((330, 160), "名称：", fontsize=10, fontname="china-s")
    page1.insert_text((365, 160), "厦门仪翔建设工程有限公司", fontsize=10, fontname="china-s")
    page1.insert_text((330, 178), "纳税人识别号：", fontsize=10, fontname="china-s")
    page1.insert_text((405, 178), "913502000658790529", fontsize=10)

    header_y = 260
    for x, text in [
        (60, "项目名称"),
        (230, "数量"),
        (290, "单价"),
        (350, "金额"),
        (420, "税率/征收率"),
        (500, "税额"),
    ]:
        page1.insert_text((x, header_y), text, fontsize=9, fontname="china-s")
    y = 282
    for name, price, tax in (
        ("*建筑服务*劳务工程款", "73933.20", "2218.00"),
        ("*建筑服务*材料款", "1200.00", "36.00"),
    ):
        page1.insert_text((60, y), name, fontsize=9, fontname="china-s")
        page1.insert_text((230, y), "1", fontsize=9)
        page1.insert_text((290, y), price, fontsize=9)
        page1.insert_text((350, y), price, fontsize=9)
        page1.insert_text((420, y), "3%", fontsize=9)
        page1.insert_text((500, y), tax, fontsize=9)
        y += 22

    page2 = doc.new_page(width=PAGE_W, height=PAGE_H)
    page2.insert_text((60, 80), "*建筑服务*安装款", fontsize=9, fontname="china-s")
    page2.insert_text((230, 80), "1", fontsize=9)
    page2.insert_text((290, 80), "300.00", fontsize=9)
    page2.insert_text((350, 80), "300.00", fontsize=9)
    page2.insert_text((420, 80), "3%", fontsize=9)
    page2.insert_text((500, 80), "9.00", fontsize=9)
    total_y = 140
    page2.insert_text((60, total_y), "合 计", fontsize=9, fontname="china-s")
    page2.insert_text((350, total_y), "¥75433.20", fontsize=9, fontname="china-s")
    page2.insert_text((500, total_y), "¥2263.00", fontsize=9, fontname="china-s")
    page2.insert_text((360, total_y + 30), "（小写）¥77696.20", fontsize=9, fontname="china-s")
    # 建筑服务信息块（跨页：备注区随合计行落在第 2 页）
    page2.insert_text((60, 220), "建筑服务发生地：", fontsize=9, fontname="china-s")
    page2.insert_text((60, 232), "福建省厦门市思明区仙岳医院", fontsize=9, fontname="china-s")
    page2.insert_text((60, 266), "建筑项目名称：", fontsize=9, fontname="china-s")
    page2.insert_text((60, 278), "厦门市仙岳医院改扩建项目", fontsize=9, fontname="china-s")

    doc.save(path)
    doc.close()


def test_cross_page_invoice_totals_on_second_page(tmp_path, invoice_engine):
    """跨页续表：明细表在第 1 页未结束、合计行与信息块在第 2 页。

    表格引擎向第 2 页拼行（items 3 行）；amount/tax_amount 的锚点在第 1 页
    表头、值在第 2 页合计行——第 1 页 totals 区域不可解析（明细行错值不可信，
    跳过），由跨页画布兜底取到合计行；信息块字段由页序兜底在第 2 页取到。
    """
    pdf = tmp_path / "cross_page.pdf"
    _make_cross_page_invoice(pdf)

    doc, values, matched, items = _process(pdf, invoice_engine)

    assert doc.status == "success", doc.error_reason
    assert doc.template_id == "invoice_v1"  # 信息块在第 2 页也能命中专属指纹
    assert len(items) == 3
    assert items[2][1] == "*建筑服务*安装款"
    assert items[2][4] == "300.00"
    # 合计字段取第 2 页的合计行（= 3 行之和，业务数学校验据此通过）
    assert Decimal(values["amount"]) == Decimal("75433.20")
    assert Decimal(values["tax_amount"]) == Decimal("2263.00")
    assert values["total_amount"] == "77696.20"
    assert values["item_rows"] == "3"
    # 信息块字段跨页取值
    assert values["construction_site"] == "福建省厦门市思明区仙岳医院"
    assert values["project_name"] == "厦门市仙岳医院改扩建项目"


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
