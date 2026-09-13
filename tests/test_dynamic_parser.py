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
def dynamic_template(contract_template):
    return {**contract_template, "mode": "dynamic"}


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

    def test_spaced_label_anchor_matches_via_merged_words(self):
        """字间距把标签切成单字（"单 位"）时，相邻词拼接后仍能命中整词锚点。"""
        from pdf.dynamic_parser import _Word, extract_anchor_field

        words = [
            _Word(0, 100, 9, 110, "单"),
            _Word(18, 100, 27, 110, "位"),
            _Word(0, 130, 18, 140, "项"),
        ]
        spec = {"anchor": "单位", "direction": "below", "max_distance": 50}

        result = extract_anchor_field("unit", words, spec)

        assert result.valid, result.errors
        assert result.normalized_value == "项"

    def test_anchor_span_extends_window_to_next_column(self):
        """"金额"表头只有两字宽、值靠右对齐超出表头范围：anchor_span 以相邻表头为右边界。

        线上症状：候选值只剩"（小写）"（它恰好压在"金额"下方），金额数字因为
        与表头 x 不重叠而被同列判定过滤掉。
        """
        from pdf.dynamic_parser import _Word, extract_anchor_field

        words = [
            _Word(350, 100, 368, 110, "金额"),          # 表头（整词，两字宽）
            _Word(425, 100, 479, 110, "税率/征收率"),   # 相邻列表头 → 右边界
            _Word(380, 130, 435, 140, "118812.57"),     # 靠右对齐：与"金额"不重叠
            _Word(352, 160, 400, 170, "（小写）"),      # 干扰项：压在表头下方
        ]
        base = {
            "anchor": "金",
            "direction": "below",
            "max_distance": 800,
            "below_mode": "row",
            "type": "decimal",
            "pattern": r"^[¥￥]?[\d,]+\.\d{2}$",
        }

        # 不开 anchor_span：候选只剩"（小写）" → 失败（与线上报错一致）
        without = extract_anchor_field("amount", words, base)
        assert not without.valid
        assert "（小写）" in without.errors[-1]

        # 开 anchor_span：x 窗口延伸到相邻表头起点，金额被采到
        with_span = extract_anchor_field("amount", words, {**base, "anchor_span": True})
        assert with_span.valid, with_span.errors
        assert with_span.normalized_value == "118812.57"

    def test_anchor_span_absorbs_letter_spaced_fragments(self):
        """"金"/"额"被字间距切成两个词时，窗口仍要推进到相邻列表头，靠右的值不能漏采。

        线上症状：金额表头被切成 "金"+"额"，旧实现把右边界取成"额"的起点 →
        靠右对齐的 118812. 57 落在窗口外 → 候选只剩压在表头下方的"（小写）"。
        """
        from pdf.dynamic_parser import _Word, extract_anchor_field

        words = [
            _Word(350, 100, 360, 110, "金"),            # 表头碎片①
            _Word(383, 100, 393, 110, "额"),            # 表头碎片②（字间距更小）
            _Word(460, 100, 514, 110, "税率/征收率"),   # 相邻列表头 → 列右边界
            _Word(396, 130, 451, 140, "118812. 57"),    # 靠右对齐，起点在"额"右侧
            _Word(352, 160, 400, 170, "（小写）"),      # 干扰项：压在表头下方
        ]
        spec = {
            "anchor": "金",
            "anchor_span": True,
            "direction": "below",
            "max_distance": 800,
            "below_mode": "row",
            "type": "decimal",
            "pattern": r"^[¥￥]?[\d,]+\.\d{2}$",
        }

        result = extract_anchor_field("amount", words, spec)

        assert result.valid, result.errors
        assert result.normalized_value == "118812.57"

    def test_anchor_span_does_not_absorb_next_column_fragment(self):
        """相邻列的表头碎片（间距更小）不应被吸收：窗口停在它的起点，避免串列。"""
        from pdf.dynamic_parser import _Word, extract_anchor_field

        words = [
            _Word(350, 100, 360, 110, "金"),           # 本列表头
            _Word(430, 100, 440, 110, "税"),           # 下一列表头碎片（离"额"更近）
            _Word(452, 100, 462, 110, "额"),
            _Word(396, 130, 451, 140, "118812. 57"),   # 本列靠右的值：必须采到
            _Word(430, 160, 462, 170, "10693.13"),     # 邻列值：不能采到
        ]
        spec = {
            "anchor": "金",
            "anchor_span": True,
            "direction": "below",
            "max_distance": 800,
            "below_mode": "row",
            "pick": "last",
            "type": "decimal",
            "pattern": r"^[¥￥]?[\d,]+\.\d{2}$",
        }

        result = extract_anchor_field("amount", words, spec)

        assert result.valid, result.errors
        assert result.normalized_value == "118812.57"  # 未取到邻列的 10693.13

    def test_real_invoice_words_amount_right_aligned(self):
        """线上实测坐标：靠右对齐的宽数值起点比单字锚点"金"还靠左，不能被守卫误杀。

        全部坐标来自实票（PyMuPDF get_text("words") 原样）：
          表头 y=151.1：项目名称 45.4 / 建筑服务发生地 119.4 / 建筑项目名称 241.3 /
                        金 405.4-414.4 / 额 423.4-432.4 / 税率/征收率 446.5 /
                        税 551.4-560.4 / 额 569.4-578.4
          明细行 y=160.9：项目名称列 *建筑服务*工程服务 12.8-93.8 /
                        金额列 118812.57 = 393.2-433.7（右缘贴"额"右缘 432.4）/
                        税率列 9% 467.5-476.5 / 税额列 10693.13 545.1-581.1
          合计行 y=258.0：¥118812.57 = 388.0-435.1，¥10693.13 = 538.5-581.1
          "合 计"标签 y=261.6：合 58.2-67.2、计 103.5-112.5（字间距 36pt，切成两词）
          价税合计 y=276.0：¥129505.70 = 440.8-496.9（只擦到金额列窗口右缘 5.7pt）

        旧实现按"锚点自身宽度"（"金" = 9pt）估算列左边界（405.4-9.0 = 396.4），
        明细行与合计行的金额（x0 分别 393.2 / 388.0）双双越界 → 被判为邻列内容
        丢弃 → 候选只剩零散词，amount 解析失败。
        """
        from pdf.dynamic_parser import _Word, extract_anchor_field

        words = [
            _Word(45.4, 151.1, 81.4, 160.1, "项目名称"),
            _Word(119.4, 151.1, 182.4, 160.1, "建筑服务发生地"),
            _Word(241.3, 151.1, 295.3, 160.1, "建筑项目名称"),
            _Word(405.4, 151.1, 414.4, 160.1, "金"),
            _Word(423.4, 151.1, 432.4, 160.1, "额"),
            _Word(446.5, 151.1, 496.1, 160.1, "税率/征收率"),
            _Word(551.4, 151.1, 560.4, 160.1, "税"),
            _Word(569.4, 151.1, 578.4, 160.1, "额"),
            _Word(12.8, 160.5, 93.8, 169.5, "*建筑服务*工程服务"),
            _Word(545.1, 160.5, 581.1, 169.5, "10693.13"),
            _Word(393.2, 160.9, 433.7, 169.9, "118812.57"),
            _Word(467.5, 160.9, 476.5, 169.9, "9%"),
            _Word(388.0, 258.0, 435.1, 271.7, "¥118812.57"),  # 合计行（金额列）
            _Word(538.5, 258.0, 581.1, 271.7, "¥10693.13"),   # 合计行（税额列）
            _Word(58.2, 261.6, 67.2, 270.6, "合"),
            _Word(103.5, 261.6, 112.5, 270.6, "计"),
            # 价税合计：不得被当成金额合计（实票里没有"（小写）"文本词）
            _Word(440.8, 276.0, 496.9, 289.8, "¥129505.70"),
        ]
        spec = {
            "anchor": "金",
            "anchor_span": True,
            "direction": "below",
            "max_distance": 800,
            "stop_anchor": ["（小写）"],
            "below_mode": "row",
            "pick": "last",
            "type": "decimal",
            "pattern": r"^[¥￥]?[\d,]+\.\d{2}$",
        }

        result = extract_anchor_field("amount", words, spec)

        assert result.valid, result.errors
        # 取"合 计"行的金额合计，而不是价税合计（129505.70）或税额（10693.13）
        assert result.normalized_value == "118812.57"

        # 同一份坐标下税额列也只取本列：明细行/合计行的 10693.13，
        # 不会被"税率/征收率"下的 9% 或金额列的值干扰
        tax = extract_anchor_field("tax_amount", words, {**spec, "anchor": "税"})
        assert tax.valid, tax.errors
        assert tax.normalized_value == "10693.13"

    def test_value_with_inner_space_matches_pattern(self):
        """文本层带字间距空格（"118812. 57" 一个词）时，pattern 校验忽略空白后仍成立。"""
        from pdf.dynamic_parser import _Word, extract_anchor_field

        words = [
            _Word(350, 100, 400, 110, "金 额"),           # 表头整串（内部带空格）
            _Word(425, 100, 479, 110, "税率/征收率"),
            _Word(380, 130, 435, 140, "118812. 57"),      # 数值整串（内部带空格）
        ]
        spec = {
            "anchor": "金",
            "anchor_span": True,
            "direction": "below",
            "max_distance": 800,
            "below_mode": "row",
            "type": "decimal",
            "pattern": r"^[¥￥]?[\d,]+\.\d{2}$",
        }

        result = extract_anchor_field("amount", words, spec)

        assert result.valid, result.errors
        assert result.normalized_value == "118812.57"

    def test_amount_split_words_joined_by_row(self):
        """金额被切词成 "118812." + "57"（小数点后有空隙）：按行拼接后应能匹配 pattern。"""
        from pdf.dynamic_parser import _Word, extract_anchor_field

        words = [
            _Word(350, 100, 368, 110, "金额"),          # 表头锚点
            _Word(350, 130, 385, 140, "118812."),       # 整数部分（落在锚点列内）
            _Word(395, 130, 406, 140, "57"),            # 小数部分（锚点列右侧碎片）
            _Word(395, 130, 406, 140, "¥118812.57"),    # 合计行（更靠下，pick=last 取它）
        ]
        spec = {
            "anchor": "金",
            "direction": "below",
            "below_mode": "row",
            "max_distance": 800,
            "pick": "last",
            "type": "decimal",
            "pattern": r"^[¥￥]?[\d,]+\.\d{2}$",
        }

        result = extract_anchor_field("amount", words, spec)

        assert result.valid, result.errors
        assert result.normalized_value == "118812.57"

    def test_row_mode_ignores_non_numeric_neighbor(self):
        """邻列的"3%"不算数值碎片，不会被并进金额。"""
        from pdf.dynamic_parser import _Word, extract_anchor_field

        words = [
            _Word(350, 100, 368, 110, "金额"),
            _Word(350, 130, 385, 140, "118812."),
            _Word(393, 130, 402, 140, "3%"),   # 紧邻的税率值：非纯数字碎片
        ]
        spec = {
            "anchor": "金",
            "direction": "below",
            "below_mode": "row",
            "max_distance": 800,
            "type": "decimal",
            "pattern": r"^[¥￥]?[\d,]+\.\d{2}$",
        }

        result = extract_anchor_field("amount", words, spec)

        assert not result.valid  # 拼接被拒 → 无合法金额，宁可失败也不出错值
        assert "118812." in result.errors[-1]

    def test_below_count_mode_stops_at_stop_anchor(self):
        """count 模式统计同列行数，遇 stop_anchor（含带间距的"合 计"）截断。"""
        from pdf.dynamic_parser import _Word, extract_anchor_field

        words = [
            _Word(0, 100, 40, 110, "项目名称"),
            _Word(0, 122, 90, 132, "*建筑服务*劳务工程款"),
            _Word(0, 144, 90, 154, "*建筑服务*劳务工程款"),
            _Word(0, 166, 9, 176, "合"),
            _Word(18, 166, 27, 176, "计"),
            _Word(0, 188, 90, 198, "建筑服务发生地："),
        ]
        spec = {
            "anchor": "项目名称",
            "direction": "below",
            "below_mode": "count",
            "max_distance": 800,
            "stop_anchor": ["合计"],
        }

        result = extract_anchor_field("item_rows", words, spec)

        assert result.valid, result.errors
        assert result.normalized_value == "2"

    def test_tax_header_letter_spaced_gap_from_real_invoice(self):
        """真票回归（发票4）：表头"税 额"切成"税"+"额"，字距 13.5pt ≈ 1.45 倍字高。

        拼接阈值 1.2 倍时"税 额/税额"锚点全部落空，级联到宽泛锚点"税"会命中
        "税率/征收率"列头，把价税合计行的 ¥140121.23（压在税率列下方）当成税额。
        阈值 1.6 倍后表头拼接命中，tax 只取本列的合计值 4081.20。
        """
        from pdf.dynamic_parser import _Word, extract_anchor_field

        words = [
            _Word(446.5, 151.1, 496.1, 160.1, "税率/征收率"),
            _Word(551.4, 151.1, 555.9, 160.1, "税"),
            _Word(569.4, 151.1, 578.4, 160.1, "额"),
            _Word(467.5, 160.9, 476.5, 169.9, "3%"),
            _Word(549.6, 160.5, 581.1, 169.5, "4081.20"),
            _Word(388.0, 259.1, 435.1, 271.5, "¥136040.03"),
            _Word(543.0, 259.1, 581.1, 271.5, "¥4081.20"),
            _Word(440.8, 276.8, 496.9, 289.6, "¥140121.23"),  # 价税合计（税率列下方）
            _Word(406.8, 280.0, 442.8, 289.0, "（小写）"),
        ]
        spec = {
            "anchor": ["税 额", "税额", "税"],
            "anchor_span": True,
            "direction": "below",
            "max_distance": 800,
            "below_mode": "row",
            "pick": "last",
            "type": "decimal",
            "pattern": r"^[¥￥]?[\d,]+\.\d{2}$",
            "scope": {"end_anchor": ["（小写）", "(小写)", "价税合计"]},
        }

        result = extract_anchor_field("tax_amount", words, spec)

        assert result.valid, result.errors
        assert result.normalized_value == "4081.20"


# ------------------------------------------------------- 兜底流程集成

class TestFallbackFlow:
    def _service(self, contract_engine):
        return PdfService(contract_engine)

    def test_fixed_success_no_fallback(self, normal_pdf, contract_engine):
        """固定解析全部成功 → 不触发兜底。"""
        engine = get_engine(":memory:")
        init_db(engine)
        service = self._service(contract_engine)
        with make_session_factory(engine)() as session:
            doc = service.process_document(session, normal_pdf)
            assert doc.status == "success"
            assert all(f.parser == "pymupdf" for f in doc.fields)

    def test_fixed_fails_dynamic_rescues(self, shifted_pdf, contract_engine):
        """布局漂移：固定 rect 落空失败 → 锚点兜底救回 → manual_review/成功。"""
        engine = get_engine(":memory:")
        init_db(engine)
        service = self._service(contract_engine)
        with make_session_factory(engine)() as session:
            doc = service.process_document(session, shifted_pdf)
            parsers = {f.field_name: f.parser for f in doc.fields}
            # 有锚点规则的字段被动态解析救回
            assert parsers["contract_no"] == "pymupdf-dynamic"
            assert parsers["customer_name"] == "pymupdf-dynamic"
            # 无锚点规则的字段仍是固定解析的失败结果
            assert parsers["amount"] == "pymupdf"
            assert doc.status in ("manual_review", "failed")

    def test_dynamic_mode_template_direct(self, shifted_pdf, contract_engine, contract_template):
        """mode=dynamic 的模板直接走动态解析，不经过固定。"""
        engine = get_engine(":memory:")
        init_db(engine)
        service = self._service(contract_engine)
        tpl = {**contract_template, "mode": "dynamic"}
        # 去掉无锚点字段与 verify 标记，聚焦纯动态解析路径（交叉验证另有管线测试）
        tpl["fields"] = {
            k: {kk: vv for kk, vv in v.items() if kk != "verify"}
            for k, v in tpl["fields"].items() if "anchor" in v
        }
        tpl["business_rules"] = []
        with make_session_factory(engine)() as session:
            doc = service.process_document(session, shifted_pdf, tpl)
            assert doc.status == "success"
            assert all(f.parser == "pymupdf-dynamic" for f in doc.fields)


# ------------------------------------------------------- Region 主搜索空间

from pdf.dynamic_parser import DynamicRegionParser, _Word  # noqa: E402


class TestRegionAnchorFallback:
    """Region 是主要搜索空间：锚点与候选先限制在 Region 内；
    Region 内找不到锚点时锚点回退全页，候选仍限制在 Region。"""

    def _words_to_pdf(self, tmp_path, words) -> str:
        doc = pymupdf.open()
        page = doc.new_page(width=595.32, height=841.92)
        for w in words:
            page.insert_text((w.x0, w.y1), w.text, fontsize=9, fontname="china-s")
        path = tmp_path / "region.pdf"
        doc.save(path)
        doc.close()
        return str(path)

    TEMPLATE = {"regions": {"totals": {"start_anchor": ["合 计"]}}}
    SPEC = {
        "page": 0,
        "region": "totals",
        "anchor": "金额",
        "direction": "below",
        "max_distance": 300,
        "type": "decimal",
    }

    def test_anchor_inside_region_preferred(self, tmp_path):
        """Region 内存在锚点 → 直接用 Region 内锚点与候选（不借全页锚点）。"""
        pdf = self._words_to_pdf(
            tmp_path,
            [
                _Word(60, 200, 104, 210, "合 计"),
                _Word(130, 200, 166, 210, "金额"),   # Region 内锚点
                _Word(130, 230, 166, 240, "333.00"),  # 锚点正下方
            ],
        )

        report = DynamicRegionParser().parse(pdf, {**self.TEMPLATE, "mode": "dynamic",
                                                   "fields": {"amount": self.SPEC}})

        assert report.fields["amount"].valid, report.fields["amount"].errors
        assert report.fields["amount"].normalized_value == "333.00"

    def test_anchor_falls_back_to_page_but_candidates_stay_in_region(self, tmp_path):
        """锚点只在 Region 外（表头）→ 锚点回退全页，候选仍限 Region：
        Region 外同锚点右侧的干扰值绝不能被取到。"""
        pdf = self._words_to_pdf(
            tmp_path,
            [
                _Word(60, 100, 96, 110, "金额"),      # Region 外锚点（表头）
                _Word(100, 100, 160, 110, "111.00"),  # 其右侧干扰值（Region 外）
                _Word(10, 200, 54, 210, "合 计"),     # Region 起点（名称列位置）
                _Word(60, 230, 96, 240, "222.00"),    # Region 内的合计值（锚点正下方）
            ],
        )

        report = DynamicRegionParser().parse(pdf, {**self.TEMPLATE, "mode": "dynamic",
                                                   "fields": {"amount": self.SPEC}})

        assert report.fields["amount"].valid, report.fields["amount"].errors
        assert report.fields["amount"].normalized_value == "222.00"
