"""表格引擎（Table First）单元与端到端测试（方案 §6-§15）。

覆盖：
  · 表头定位（整词 / 字间距拆词拼接）
  · 列边界（相邻表头中心点取中）
  · 表格上下边界（表头底部 ~ stop_anchor）
  · 物理行聚类 → 逻辑行重建（跨行名称合并）
  · items 输出（行关联关系在解析时建立）与字段回填

运行：.venv/Scripts/python.exe -m pytest tests/test_table_engine.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pdf.dynamic_parser import DynamicRegionParser, _Word
from pdf.table_engine import extract_table, extract_table_multipage
from pdf.template_engine import TemplateEngine
from pdf.validators import FieldResult, validate_business, validate_structure

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"


def _template(template_id: str = "invoice_v1") -> dict:
    """加载合并 base 后的完整模板（与生产管线同构，table/regions 来自 base）。"""
    return TemplateEngine(TEMPLATES_DIR).get(template_id) or {}


def _table_config(template_id: str = "invoice_v1") -> dict:
    return _template(template_id)["table"]


def _header(y: float = 251.0) -> list[_Word]:
    return [
        _Word(60, y, 96, y + 9, "项目名称"),
        _Word(230, y, 248, y + 9, "数量"),
        _Word(290, y, 308, y + 9, "单价"),
        _Word(350, y, 368, y + 9, "金额"),
        _Word(420, y, 474, y + 9, "税率/征收率"),
        _Word(500, y, 518, y + 9, "税额"),
    ]


def _data_row(
    y: float,
    name: str = "*建筑服务*劳务工程款",
    *,
    quantity: str = "1",
    unit_price: str = "73933.20",
    amount: str = "73933.20",
    tax: str = "2218.00",
    rate: str = "3%",
) -> list[_Word]:
    return [
        _Word(60, y, 60 + len(name) * 9, y + 9, name),
        _Word(230, y, 235, y + 9, quantity),
        _Word(290, y, 335, y + 9, unit_price),
        _Word(350, y, 395, y + 9, amount),
        _Word(420, y, 429, y + 9, rate),
        _Word(500, y, 536, y + 9, tax),
    ]


def _totals_row(y: float) -> list[_Word]:
    return [
        _Word(60, y, 76, y + 9, "合 计"),
        _Word(350, y, 400, y + 9, "¥73933.20"),
        _Word(500, y, 550, y + 9, "¥2218.00"),
    ]


# ----------------------------------------------------------- 表头与列边界


class TestHeaderAndColumns:
    def test_header_found_and_bounds_between_centers(self):
        words = [*_header(), *_data_row(273), *_totals_row(360)]

        result = extract_table(words, _table_config())

        assert result.ok, result.issues
        assert [key for key, col in result.columns.items() if col.present] == [
            "name",
            "quantity",
            "unit_price",
            "amount",
            "tax_rate",
            "tax",
        ]
        # 列边界 = 相邻表头中心点取中（单价中心 299 / 金额中心 359 → 329）
        amount_col = result.columns["amount"]
        assert amount_col.left == pytest.approx((299 + 359) / 2)
        assert amount_col.right == pytest.approx((359 + 447) / 2)

    def test_letter_spaced_header_merged_in_band(self):
        """"金"+"额"被字间距切成两个词时，表头带内拼接后仍能命中。"""
        words = [
            _Word(350, 251, 359, 260, "金"),
            _Word(368, 251, 377, 260, "额"),
            _Word(60, 251, 96, 260, "项目名称"),
            _Word(230, 251, 248, 260, "数量"),
            _Word(290, 251, 308, 260, "单价"),
            _Word(420, 251, 474, 260, "税率/征收率"),
            _Word(500, 251, 518, 260, "税额"),
            *_data_row(273),
            *_totals_row(360),
        ]

        result = extract_table(words, _table_config())

        assert result.ok, result.issues
        assert result.columns["amount"].present
        assert result.items[0]["amount"] == "73933.20"

    def test_missing_header_returns_issue(self):
        words = [_Word(60, 251, 96, 260, "说明"), _Word(60, 273, 96, 282, "正文")]

        result = extract_table(words, _table_config())

        assert not result.ok
        assert "table_header_not_found" in result.issues
        assert result.items == []

    def test_total_labels_are_not_treated_as_detail_headers(self):
        words = [
            _Word(350, 251, 400, 260, "合计金额"),
            _Word(500, 251, 550, 260, "合计税额"),
            _Word(350, 273, 400, 282, "850778.42"),
            _Word(500, 273, 550, 282, "110601.20"),
        ]

        result = extract_table(words, _table_config())

        assert not result.ok
        assert "table_header_not_found" in result.issues
        assert result.items == []

    def test_single_column_label_is_not_enough_to_start_table(self):
        words = [
            _Word(350, 251, 368, 260, "金额"),
            _Word(350, 273, 400, 282, "850778.42"),
        ]

        result = extract_table(words, _table_config())

        assert not result.ok
        assert "table_header_not_found" in result.issues

    def test_missing_optional_column_flagged(self):
        """规格型号/单位列不存在：列标 missing（可选列不影响 items）。"""
        words = [*_header(), *_data_row(273), *_totals_row(360)]

        result = extract_table(words, _table_config())

        assert "table_column_missing:spec" in result.issues
        assert "table_column_missing:unit" in result.issues
        assert len(result.items) == 1


# ----------------------------------------------------------- 物理行 → 逻辑行


class TestLogicalRows:
    def test_multiple_data_rows_are_separate_items(self):
        words = [
            *_header(),
            *_data_row(273, amount="100.00"),
            *_data_row(295, amount="200.00"),
            *_data_row(317, amount="300.00"),
            *_totals_row(360),
        ]

        result = extract_table(words, _table_config())

        assert len(result.items) == 3
        assert [item["amount"] for item in result.items] == ["100.00", "200.00", "300.00"]
        assert [item["row_index"] for item in result.items] == [1, 2, 3]

    def test_close_rows_are_split_by_repeated_numeric_y_layers(self):
        """两行 y0 差小于初始容差时，多个数值列的 y 层可将其恢复为两行。"""
        words = [
            *_header(),
            *_data_row(273, name="*服务*甲", amount="100.00", rate="20%"),
            *_data_row(276, name="*服务*乙", amount="200.00", rate="13%"),
            *_totals_row(360),
        ]

        result = extract_table(words, _table_config())

        assert len(result.items) == 2
        assert [item["name"] for item in result.items] == ["*服务*甲", "*服务*乙"]
        assert [item["tax_rate"] for item in result.items] == ["20%", "13%"]
        assert "20%13%" not in json.dumps(result.items, ensure_ascii=False)

    def test_wrapped_name_merged_into_next_data_row(self):
        """名称折成两行（第二行才带数值）：合并为一条明细（方案 §13/§14）。"""
        words = [
            *_header(),
            _Word(60, 273, 96, 282, "建筑服务"),
            _Word(60, 295, 150, 304, "某某道路工程"),
            _Word(230, 295, 235, 304, "2"),
            _Word(290, 295, 335, 304, "100.00"),
            _Word(350, 295, 395, 304, "200.00"),
            _Word(420, 295, 429, 304, "9%"),
            _Word(500, 295, 536, 304, "18.00"),
            *_totals_row(360),
        ]

        result = extract_table(words, _table_config())

        assert len(result.items) == 1
        item = result.items[0]
        assert item["name"] == "建筑服务某某道路工程"
        assert item["quantity"] == "2"
        assert item["unit_price"] == "100.00"
        assert item["amount"] == "200.00"
        assert item["tax_rate"] == "9%"
        assert item["tax"] == "18.00"

    def test_pending_name_not_carried_across_items(self):
        """名称折行只并入紧随其后的数据行，不会串到后续明细。"""
        words = [
            *_header(),
            _Word(60, 273, 96, 282, "前缀说明"),
            *_data_row(295, name="*服务*甲", amount="100.00"),
            *_data_row(317, name="*服务*乙", amount="200.00"),
            *_totals_row(360),
        ]

        result = extract_table(words, _table_config())

        assert [item["name"] for item in result.items] == ["前缀说明*服务*甲", "*服务*乙"]

    def test_rate_only_continuation_is_not_carried_into_next_row(self):
        """非数据行的税率不得与下一条明细拼成 20%20%。"""
        words = [
            *_header(),
            _Word(420, 273, 440, 282, "20%"),
            *_data_row(295, rate="20%"),
            *_totals_row(360),
        ]

        result = extract_table(words, _table_config())

        assert result.items[0]["tax_rate"] == "20%"
        assert "20%20%" not in json.dumps(result.items, ensure_ascii=False)
        assert "table_trailing_rows" in result.issues

    def test_info_block_rows_do_not_become_items(self):
        """表格下方信息块（长文本落进数值列）不构成明细行（行级合理性守卫）。"""
        words = [
            *_header(),
            *_data_row(273),
            _Word(60, 300, 130, 309, "建筑服务发生地:"),
            _Word(60, 312, 240, 321, "福建省厦门市湖里区仙岳医院院区"),
            *_totals_row(360),
        ]
        config = _table_config()
        # 模拟没有把信息块标签配进 stop_anchor 的版式：靠行级守卫兜住噪声
        config = {**config, "stop_anchor": ["合 计", "合计", "价税合计"]}

        result = extract_table(words, config)

        assert len(result.items) == 1
        assert result.items[0]["amount"] == "73933.20"


# ----------------------------------------------------------- 单元格与边界


class TestCellsAndBounds:
    def test_split_decimal_fragments_joined_within_cell(self):
        words = [
            *_header(),
            _Word(60, 273, 150, 282, "*服务*工程"),
            _Word(230, 273, 235, 282, "1"),
            _Word(290, 273, 335, 282, "73933.20"),
            # 金额列的值被切词成两段：整数段 + 小数段（都在金额列区间内）
            _Word(350, 273, 393, 282, "73933."),
            _Word(394, 273, 404, 282, "20"),
            _Word(420, 273, 429, 282, "3%"),
            _Word(500, 273, 536, 282, "2218.00"),
        ]

        result = extract_table(words, _table_config())

        assert len(result.items) == 1
        assert result.items[0]["amount"] == "73933.20"
        assert result.items[0]["unit_price"] == "73933.20"

    def test_placeholder_cell_is_empty(self):
        words = [
            *_header(),
            _Word(60, 273, 150, 282, "*服务*工程"),
            _Word(165, 273, 174, 282, "—"),
            _Word(230, 273, 235, 282, "1"),
            _Word(290, 273, 335, 282, "100.00"),
            _Word(350, 273, 395, 282, "100.00"),
            _Word(420, 273, 429, 282, "3%"),
            _Word(500, 273, 536, 282, "3.00"),
        ]
        config = _table_config()
        config["columns"]["spec"] = {"title": "规格型号", "headers": ["规格型号"]}

        result = extract_table(words, config)

        assert result.items[0]["spec"] is None

    def test_two_rates_in_merged_physical_row_are_ambiguous(self):
        """两行距离过近被聚成一行时，税率置空并留下结构风险，而不是直接拼接。"""
        words = [
            *_header(),
            *_data_row(273, rate="20%"),
            _Word(420, 275, 440, 284, "20%"),
            *_totals_row(360),
        ]

        result = extract_table(words, _table_config())

        assert result.items[0]["tax_rate"] is None
        assert "table_cell_ambiguous:1:tax_rate" in result.issues
        checks = validate_structure(
            _template(), _Report({}, items=result.items, table=result)
        )
        ambiguity = next(c for c in checks if c.rule == "table_cell_ambiguity")
        assert ambiguity.failed

    def test_stop_takes_min_across_direct_and_merged_hits(self):
        """真票回归（发票1）：直匹配命中下方"价税合计（大写）"时，仍必须取上方
        只有整行拼接才能命中的"合 计"行——逐级短路会把合计/价税合计行混进明细。"""
        words = [
            *_header(),
            *_data_row(273),
            _Word(58.2, 295.6, 67.2, 304.6, "合"),           # "合 计"切成两词、大间距
            _Word(103.5, 295.6, 112.5, 304.6, "计"),
            _Word(388.0, 293.1, 435.1, 305.5, "¥73933.20"),
            _Word(543.0, 293.1, 581.1, 305.5, "¥2218.00"),
            _Word(48.0, 312.0, 120.0, 321.0, "价税合计（大写）"),  # 直匹配命中（更下方）
            _Word(178.4, 310.1, 313.4, 319.1, "壹拾肆万柒仟肆佰壹拾贰圆整"),
            _Word(406.8, 312.0, 442.8, 321.0, "（小写）"),
            _Word(440.8, 308.8, 496.9, 321.6, "¥147412.00"),
        ]

        result = extract_table(words, _table_config())

        assert result.stop_y == pytest.approx(293.1)  # "合 计"行（拼接命中），非价税合计行
        assert len(result.items) == 1
        assert result.items[0]["amount"] == "73933.20"
        assert all("合计" not in str(item.get("name") or "") for item in result.items)
        assert all(item.get("tax") != "147412.00" for item in result.items)

    def test_totals_row_excluded_from_items(self):
        words = [*_header(), *_data_row(273), *_totals_row(360)]

        result = extract_table(words, _table_config())

        assert result.stop_y == pytest.approx(360)
        assert all(item["amount"] != "73933.20" or item["row_index"] == 1 for item in result.items)
        assert len(result.items) == 1

    def test_table_as_dict_is_serialisable(self):
        words = [*_header(), *_data_row(273), *_totals_row(360)]

        result = extract_table(words, _table_config())
        payload = json.loads(json.dumps(result.as_dict(), ensure_ascii=False))

        assert payload["ok"] is True
        assert payload["items"][0]["name"] == "*建筑服务*劳务工程款"
        assert payload["column_count"] == 6


# ----------------------------------------------------------- 端到端（解析管线）


class TestEndToEnd:
    def test_v1_invoice_items_and_field_backfill(self, tmp_path):
        from tests.test_invoice_multiline import _make_invoice

        pdf = tmp_path / "inv_v1.pdf"
        _make_invoice(pdf, rows=3)

        template, kind = TemplateEngine(TEMPLATES_DIR).resolve(str(pdf))
        assert kind == "match" and template["template"] == "invoice_v1"

        report = DynamicRegionParser().parse(str(pdf), template)

        assert len(report.items) == 3, report.table.issues
        assert report.items[0]["name"] == "*建筑服务*劳务工程款"
        assert report.items[0]["tax"] == "2218.00"
        # 基本信息块不计入明细（stop 边界在信息块标签处）
        assert all("建筑服务发生地" not in (item["name"] or "") for item in report.items)
        # 明细字段由表格结果回填（parser 标记可审计）
        assert report.fields["item_name"].parser == "pymupdf-table"
        assert report.fields["item_name"].normalized_value == "*建筑服务*劳务工程款"
        assert report.fields["item_rows"].normalized_value == "3"
        assert report.fields["item_amount_rows"].normalized_value == "3"
        assert report.fields["item_tax_rows"].normalized_value == "3"
        for name in ("quantity", "unit_price", "tax_rate"):
            assert report.fields[name].parser == "pymupdf-table", name

    def test_v2_variant_items_include_unit_column(self, tmp_path):
        from tests.test_invoice_variant_columns import _make_variant_invoice

        pdf = tmp_path / "inv_v2.pdf"
        _make_variant_invoice(pdf, with_construction_block=False, rows=2)

        template, kind = TemplateEngine(TEMPLATES_DIR).resolve(str(pdf))
        assert kind == "match" and template["template"] == "invoice_v2"

        report = DynamicRegionParser().parse(str(pdf), template)

        assert len(report.items) == 2, report.table.issues
        assert report.items[0]["unit"] == "项"
        assert report.items[0]["spec"] is None  # "—"占位符
        assert report.fields["unit"].normalized_value == "项"

    def test_generic_template_items(self, tmp_path):
        from tests.test_generic_fallback import _make_legacy_invoice

        pdf = tmp_path / "inv_generic.pdf"
        _make_legacy_invoice(pdf)

        template, kind = TemplateEngine(TEMPLATES_DIR).resolve(str(pdf))
        assert kind == "fallback" and template["template"] == "invoice_generic"

        report = DynamicRegionParser().parse(str(pdf), template)

        assert len(report.items) == 1, report.table.issues
        assert report.items[0]["name"] == "*建筑服务*劳务工程款"
        assert report.items[0]["unit"] == "项"

    def test_table_failure_keeps_anchor_fields(self, tmp_path):
        """表格重建失败（无明细表头）时明细字段保留锚点取值，不被清空。"""
        import pymupdf

        pdf = tmp_path / "no_table.pdf"
        doc = pymupdf.open()
        page = doc.new_page(width=595.32, height=841.92)
        page.insert_text((60, 60), "电子发票（普通发票）", fontsize=16, fontname="china-s")
        page.insert_text((330, 90), "发票号码：", fontsize=10, fontname="china-s")
        page.insert_text((395, 90), "26942000000871475416", fontsize=10)
        doc.save(pdf)
        doc.close()

        template = _template("invoice_v2")
        template = {**template, "identify": {"any": ["电子发票"]}}

        report = DynamicRegionParser().parse(str(pdf), template)

        assert report.table is not None and not report.table.ok
        assert "table_header_not_found" in report.table.issues
        assert report.table_applied == []


# ----------------------------------------------------------- 边缘收口与归属


class TestEdgeMargin:
    def test_outside_words_excluded_by_edge_margin(self):
        """表格左右两侧的页边/备注文本不得被吸进首列/末列（无无限边界）。"""
        words = [
            _Word(10, 273, 30, 282, "页边"),   # 表格左侧外（首列表头 x0=60）
            *_header(),
            *_data_row(273),
            _Word(560, 273, 590, 282, "页脚"),  # 表格右侧外（末列表头 x1=518）
            *_totals_row(360),
        ]

        result = extract_table(words, _table_config())

        payload = json.dumps(result.items, ensure_ascii=False)
        assert result.ok
        assert result.items[0]["name"] == "*建筑服务*劳务工程款"
        assert "页边" not in payload and "页脚" not in payload

    def test_edge_margin_configurable(self):
        """edge_margin=0 时以表头边缘为界：贴边文本仍排除，列内值不受影响。"""
        words = [
            _Word(30, 273, 50, 282, "贴边"),
            *_header(),
            *_data_row(273),
            *_totals_row(360),
        ]
        config = {**_table_config(), "edge_margin": 0}

        result = extract_table(words, config)

        assert "贴边" not in json.dumps(result.items, ensure_ascii=False)
        assert result.items[0]["name"] == "*建筑服务*劳务工程款"


class TestLogicalRowAffiliation:
    def test_suffix_continuation_joins_previous_item(self):
        """表尾名称行（后面没有数据行）→ suffix：并入上一条明细（方案：附加说明
        属于上一行，而不是下一条商品）。"""
        words = [
            *_header(),
            *_data_row(273),
            _Word(60, 300, 150, 309, "含安装调试费"),
            *_totals_row(360),
        ]

        result = extract_table(words, _table_config())

        assert len(result.items) == 1
        assert result.items[0]["name"].endswith("含安装调试费")
        assert result.items[0]["amount"] == "73933.20"
        assert "table_trailing_rows" not in result.issues

    def test_prefix_takes_priority_when_data_follows(self):
        """名称行后跟数据行 → 仍是 prefix（并入下一条），suffix 只兜表尾。"""
        words = [
            *_header(),
            _Word(60, 263, 96, 272, "前置说明"),  # 表头底部与首条数据行之间
            *_data_row(273),
            _Word(60, 300, 150, 309, "含安装调试费"),
            *_totals_row(360),
        ]

        result = extract_table(words, _table_config())

        assert len(result.items) == 1
        assert result.items[0]["name"].startswith("前置说明")
        assert result.items[0]["name"].endswith("含安装调试费")

    def test_trailing_without_suffix_flag_is_noise(self):
        """suffix_continuation=false：表尾残余按 standalone noise 留痕。"""
        words = [
            *_header(),
            *_data_row(273),
            _Word(60, 300, 150, 309, "含安装调试费"),
            *_totals_row(360),
        ]
        config = {**_table_config(), "suffix_continuation": False}

        result = extract_table(words, config)

        assert len(result.items) == 1
        assert result.items[0]["name"] == "*建筑服务*劳务工程款"
        assert "table_trailing_rows" in result.issues

    def test_suffix_never_pollutes_numeric_columns(self):
        """suffix 只并入 string 列：数值列残余不并入（避免污染数量/金额）。"""
        words = [
            *_header(),
            *_data_row(273),
            # 表尾残余：名称列说明文字 + 数量列长文本（守卫拦截后不被 suffix 并入）
            _Word(60, 300, 150, 309, "含安装调试费"),
            _Word(230, 300, 320, 309, "一批量大文本说明内容"),
            *_totals_row(360),
        ]

        result = extract_table(words, _table_config())

        assert result.items[0]["quantity"] == "1"  # 未被污染
        assert result.items[0]["name"].endswith("含安装调试费")
        assert "table_trailing_rows" in result.issues  # 数值残余按 noise 留痕


# ----------------------------------------------------------- 第二种表头（真票样式）


class TestVariantHeaderLayout:
    """第二种表头：项目名称｜规格型号｜单 位｜数 量｜单 价｜金 额｜税率/征收率｜税 额。

    字间距表头两种切词形态都要命中：整词带空格（"单 位"）与拆成单字
    （"单"+"位"，字距约一倍字宽，靠相邻词拼接兜底）。
    """

    def _v2_header(self, split: bool) -> list[_Word]:
        y = 251.0
        heads = [
            (60, "项目名称"),
            (165, "规格型号"),
            (222, "单 位"),
            (265, "数 量"),
            (310, "单 价"),
            (365, "金 额"),
            (425, "税率/征收率"),
            (510, "税 额"),
        ]
        words: list[_Word] = []
        for x, text in heads:
            if split and " " in text:
                a, b = text.split(" ")
                words.append(_Word(x, y, x + 9, y + 9, a))
                words.append(_Word(x + 18, y, x + 27, y + 9, b))
            else:
                words.append(_Word(x, y, x + len(text) * 9, y + 9, text))
        return words

    def _rows(self) -> list[_Word]:
        return [
            _Word(60, 273, 150, 282, "*建筑服务*劳务工程款"),
            _Word(165, 273, 174, 282, "—"),
            _Word(222, 273, 231, 282, "项"),
            _Word(265, 273, 270, 282, "1"),
            _Word(310, 273, 355, 282, "73933.20"),
            _Word(365, 273, 410, 282, "73933.20"),
            _Word(425, 273, 434, 282, "3%"),
            _Word(510, 273, 546, 282, "2218.00"),
            _Word(60, 295.6, 76, 304.6, "合 计"),
            _Word(365, 295.6, 410, 304.6, "¥73933.20"),
            _Word(510, 295.6, 546, 304.6, "¥2218.00"),
        ]

    @pytest.mark.parametrize("split", [False, True])
    def test_variant_header_columns_and_items(self, split):
        words = [*self._v2_header(split), *self._rows()]

        result = extract_table(words, _table_config("invoice_v2"))

        assert result.ok, result.issues
        assert {key for key, col in result.columns.items() if col.present} == {
            "name",
            "spec",
            "unit",
            "quantity",
            "unit_price",
            "amount",
            "tax_rate",
            "tax",
        }
        item = result.items[0]
        assert item["name"] == "*建筑服务*劳务工程款"
        assert item["spec"] is None  # "—"占位符
        assert item["unit"] == "项"
        assert item["quantity"] == "1"
        assert item["unit_price"] == "73933.20"
        assert item["amount"] == "73933.20"
        assert item["tax_rate"] == "3%"
        assert item["tax"] == "2218.00"
        assert "table_trailing_rows" not in result.issues


# ----------------------------------------------------------- 校验（items 模式）


class _Report:
    """最小解析报告替身：utils 校验只需要 fields / items / table。"""

    def __init__(self, fields: dict, items: list | None = None, table=None):
        self.fields = fields
        self.items = items or []
        self.table = table


def _field(name: str, value: str | None) -> FieldResult:
    return FieldResult(field_name=name, raw_value=value or "", normalized_value=value)


def _two_rows() -> list:
    """两行自洽明细：1×100.00=100.00（税 3.00）+ 1×200.00=200.00（税 6.00）。"""
    words = [
        *_header(),
        *_data_row(273, unit_price="100.00", amount="100.00", tax="3.00"),
        *_data_row(295, unit_price="200.00", amount="200.00", tax="6.00"),
        *_totals_row(360),
    ]
    return extract_table(words, _table_config()).items


def _items_report(items, *, amount: str | None = "300.00", tax: str | None = "9.00",
                  grand: str | None = "309.00", table=None) -> _Report:
    return _Report(
        fields={
            "item_name": _field("item_name", items[0]["name"] if items else None),
            "amount": _field("amount", amount),
            "tax_amount": _field("tax_amount", tax),
            "total_amount": _field("total_amount", grand),
            "item_rows": _field("item_rows", str(len(items))),
        },
        items=items,
        table=table,
    )


class TestItemsValidation:
    def test_structure_table_mode_passes(self):
        items = _two_rows()
        table = extract_table(
            [*_header(), *_data_row(273, amount="100.00"), *_data_row(295, amount="200.00"), *_totals_row(360)],
            _table_config(),
        )

        checks = validate_structure(_template(), _items_report(items, table=table))

        assert all(check.passed for check in checks), [c.as_dict() for c in checks]
        assert {check.rule for check in checks} >= {"table_structure", "table_columns"}

    def test_structure_flags_missing_required_column(self):
        """金额列缺失（必需列）→ 结构异常（table_structure_inconsistent 语义）。"""
        words = [
            _Word(60, 251, 96, 260, "项目名称"),
            _Word(230, 251, 248, 260, "数量"),
            _Word(500, 251, 518, 260, "税额"),
            _Word(60, 273, 150, 282, "*服务*工程"),
            _Word(230, 273, 235, 282, "1"),
            _Word(500, 273, 536, 282, "3.00"),
        ]
        table = extract_table(words, _table_config())
        checks = validate_structure(_template(), _items_report(table.items, table=table))

        failed = [c for c in checks if c.failed]
        # 金额列缺失 → 列级异常 + 该列的逐行关键单元格必然缺失
        assert [c.rule for c in failed] == ["table_columns", "item_cell_amount"]
        assert "金额" in failed[0].detail

    def test_structure_flags_incomplete_item_cell(self):
        """逐行关键单元格缺失（第 2 行没有金额）→ 结构异常。"""
        items = _two_rows()
        items[1]["amount"] = None
        table = extract_table(
            [*_header(), *_data_row(273, amount="100.00"), *_data_row(295, amount="200.00"), *_totals_row(360)],
            _table_config(),
        )

        checks = validate_structure(_template(), _items_report(items, table=table))

        failed = [c for c in checks if c.failed]
        assert [c.rule for c in failed] == ["item_cell_amount"]
        assert "第 2 行" in failed[0].detail

    def test_structure_flags_unrebuilt_table(self):
        """表格未重建（无 items）→ 结构异常，提示回退锚点人工核对。"""
        table = extract_table([_Word(60, 100, 96, 109, "无关文本")], _table_config())

        checks = validate_structure(_template(), _items_report([], table=table))

        failed = [c for c in checks if c.failed]
        assert [c.rule for c in failed] == ["table_structure"]
        assert "未重建成功" in failed[0].detail

    def test_business_items_all_pass(self):
        items = _two_rows()  # 每行 1×73933.20=73933.20、税额 2218.00（3%）

        checks = validate_business(_template(), _items_report(items))

        assert all(check.passed for check in checks), [c.as_dict() for c in checks]
        assert {check.rule for check in checks} == {
            "item_amount",
            "item_tax",
            "sum_amount",
            "sum_tax",
            "grand_total",
        }

    def test_business_detects_bad_row(self):
        """某行数量×单价≠金额 → 逐行校验失败（明显不成立 → error 级）。"""
        items = _two_rows()
        items[1]["amount"] = "999.00"

        checks = validate_business(_template(), _items_report(items))

        item = next(c for c in checks if c.rule == "item_amount")
        assert item.failed and item.severity == "error"
        assert "第2行" in item.detail

    def test_business_detects_sum_mismatch(self):
        """Σ明细金额 ≠ 合计金额 → 累加校验失败（尾差 warning / 明显不成立 error）。"""
        items = _two_rows()

        checks = validate_business(_template(), _items_report(items, amount="100.00"))

        total = next(c for c in checks if c.rule == "sum_amount")
        assert total.failed and total.severity == "error"
        assert "Σ明细金额" in total.detail
        assert "明细：100.00 + 200.00" in total.detail

    def test_business_skips_sums_when_rows_incomplete(self):
        """行内缺金额 → Σ 校验记 skipped（留痕不误报），逐行校验只覆盖完整行。"""
        items = _two_rows()
        items[1]["amount"] = None

        checks = validate_business(_template(), _items_report(items))

        assert next(c for c in checks if c.rule == "sum_amount").skipped
        item = next(c for c in checks if c.rule == "item_amount")
        assert item.passed and not item.skipped  # 仅第 1 行参与校验


# ----------------------------------------------------------- 明细表跨页拼接


class TestMultipageTable:
    """两页明细表：首页贴到页底（未命中合计锚点）→ 后续页拼行接续。"""

    def test_single_page_complete_table_not_extended(self):
        """首页命中合计锚点（表格闭合）→ 不向后页拼接。"""
        page1 = [*_header(), *_data_row(273), *_totals_row(360)]
        page2 = [*_data_row(60, name="*建筑服务*安装款"), *_totals_row(120)]

        result = extract_table_multipage(iter([page1, page2]), _table_config())

        assert result.ok, result.issues
        assert [it["row_index"] for it in result.items] == [1]

    def test_single_page_equals_plain_extract(self):
        """单页文档经跨页入口与单页入口结果完全一致：跨页逻辑不影响单页。"""
        page = [*_header(), *_data_row(273), *_totals_row(360)]

        plain = extract_table(page, _table_config())
        multipage = extract_table_multipage(iter([page]), _table_config())

        assert multipage.items == plain.items
        assert multipage.stop_y == plain.stop_y
        assert multipage.header_y == plain.header_y
        assert multipage.issues == plain.issues

    def test_single_page_open_table_not_extended_without_more_pages(self):
        """单页且表格贴页底（无合计行）：iterator 已耗尽，不再扩展也不报错。"""
        page = [*_header(), *_data_row(273)]

        result = extract_table_multipage(iter([page]), _table_config())

        assert [it["row_index"] for it in result.items] == [1]
        assert result.stop_y is None

    def test_rows_continue_on_next_page(self):
        """首页表格贴到页底（无合计行）→ 第 2 页行拼接、行号接续。"""
        page1 = [
            *_header(),
            *_data_row(273),
            *_data_row(300, name="*建筑服务*材料款", unit_price="1200.00", amount="1200.00", tax="36.00"),
        ]
        page2 = [
            *_data_row(60, name="*建筑服务*安装款", unit_price="300.00", amount="300.00", tax="9.00"),
            *_totals_row(120),
        ]

        result = extract_table_multipage(iter([page1, page2]), _table_config())

        assert result.ok, result.issues
        assert [it["row_index"] for it in result.items] == [1, 2, 3]
        assert result.items[2]["amount"] == "300.00"
        assert result.items[2]["tax"] == "9.00"
        assert result.items[1]["amount"] == "1200.00"

    def test_continuation_stops_at_totals_on_second_page(self):
        """第 2 页命中合计锚点 → 合计行之后的文本不进入明细。"""
        page1 = [*_header(), *_data_row(273)]
        page2 = [
            *_data_row(60, name="*建筑服务*安装款", unit_price="300.00", amount="300.00", tax="9.00"),
            *_totals_row(120),
            _Word(60, 160, 150, 169, "备注说明文本"),
        ]

        result = extract_table_multipage(iter([page1, page2]), _table_config())

        assert [it["row_index"] for it in result.items] == [1, 2]
        assert all("备注" not in (it.get("name") or "") for it in result.items)

    def test_continuation_stops_when_page_has_no_data_rows(self):
        """后续页拼不出明细行（表格已结束）→ 停止扩展。"""
        page1 = [*_header(), *_data_row(273)]
        page2 = [_Word(60, 60, 150, 69, "尾页只有说明文字")]

        result = extract_table_multipage(iter([page1, page2]), _table_config())

        assert [it["row_index"] for it in result.items] == [1]

    def test_empty_page_iterator(self):
        iterator = iter([])

        result = extract_table_multipage(iterator, _table_config())

        assert not result.ok
        assert "table_not_configured" in result.issues

    def test_totals_only_on_second_page_closes_table(self):
        """明细全在第 1 页、合计行在第 2 页 → 表格闭合（stop_y 回写、明细不丢）。"""
        page1 = [*_header(), *_data_row(273), *_data_row(300, name="*建筑服务*材料款")]
        page2 = [*_totals_row(140)]

        result = extract_table_multipage(iter([page1, page2]), _table_config())

        assert [it["row_index"] for it in result.items] == [1, 2]
        assert result.stop_y == pytest.approx(140)

    def test_continuation_ignores_repeated_header_and_page_footer(self):
        """后续页的噪声：页眉 + 重复表头行 + 页脚都不进明细。

        不过滤时：重复表头行会因"短文本即数据"变成垃圾 item（name="项目名称"），
        页眉文字会被 prefix 并入下一条明细的名称。
        """
        page1 = [*_header(), *_data_row(273), *_data_row(300, name="*建筑服务*材料款")]
        page2 = [
            _Word(60, 40, 150, 49, "销货清单（续）"),  # 页眉（在重复表头上方）
            *_header(60),  # 重复表头行
            *_data_row(82, name="*建筑服务*安装款", unit_price="300.00", amount="300.00", tax="9.00"),
            *_totals_row(140),
            _Word(290, 800, 310, 809, "1/2"),  # 页脚页码
        ]

        result = extract_table_multipage(iter([page1, page2]), _table_config())

        assert [it["row_index"] for it in result.items] == [1, 2, 3]
        assert [it["name"] for it in result.items] == [
            "*建筑服务*劳务工程款",
            "*建筑服务*材料款",
            "*建筑服务*安装款",
        ]
        assert result.stop_y == pytest.approx(140)  # 合计行在本页：表格闭合
        assert all("销货清单" not in (it["name"] or "") for it in result.items)
        assert all(it["quantity"] == "1" for it in result.items)

    def test_continuation_split_word_header_not_turned_into_item(self):
        """第 2 页表头被字间距拆词（"数"+"量"）：文本不匹配也能靠位置判据识别。"""
        split_header = [
            _Word(60, 60, 96, 69, "项目名称"),
            _Word(230, 60, 239, 69, "数"),
            _Word(248, 60, 257, 69, "量"),
            _Word(290, 60, 299, 69, "单"),
            _Word(308, 60, 317, 69, "价"),
            _Word(350, 60, 359, 69, "金"),
            _Word(368, 60, 377, 69, "额"),
            _Word(420, 60, 474, 69, "税率/征收率"),
            _Word(500, 60, 509, 69, "税"),
            _Word(518, 60, 527, 69, "额"),
        ]
        page1 = [*_header(), *_data_row(273)]
        page2 = [
            *split_header,
            *_data_row(82, name="*建筑服务*安装款", unit_price="300.00", amount="300.00", tax="9.00"),
            *_totals_row(140),
        ]

        result = extract_table_multipage(iter([page1, page2]), _table_config())

        assert [it["row_index"] for it in result.items] == [1, 2]
        assert result.items[1]["name"] == "*建筑服务*安装款"
        assert result.items[1]["quantity"] == "1"

    def test_single_page_footer_not_turned_into_item(self):
        """单页表格贴到页底（无合计行）时，页脚页码不进明细。"""
        words = [
            *_header(),
            *_data_row(273),
            _Word(290, 800, 310, 809, "1/2"),  # 页脚：落在数量/单价列位置
        ]

        result = extract_table(words, _table_config())

        assert len(result.items) == 1
        assert result.items[0]["name"] == "*建筑服务*劳务工程款"
