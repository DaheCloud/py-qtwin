"""模板指纹识别（方案 §18-§19）与 Base Schema + Variant（方案 §16）测试。

覆盖：
  · 指纹打分：文本 30 + 锚点位置 30 + 表头结构 30 + 页面特征 10 → 0~100
  · area 位置语义（top_right / left / …）与老写法兼容
  · extends 合并：fields 深合并（模板覆盖 base），公共配置继承
  · Variant：detect 命中 → required_fields 切换字段严重程度

运行：.venv/Scripts/python.exe -m pytest tests/test_template_fingerprint.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pdf.template_engine import (
    DocFingerprint,
    TemplateEngine,
    anchor_match,
    evaluate_fingerprint,
    evaluate_template,
    header_match,
    is_fingerprint,
)
from pdf.words import Word

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"

PAGE_W, PAGE_H = 595.32, 841.92


def _fp(words: list[Word], text: str | None = None) -> DocFingerprint:
    return DocFingerprint(
        text=text if text is not None else " ".join(w.text for w in words),
        words=tuple(words),
        page_width=PAGE_W,
        page_height=PAGE_H,
    )


# ----------------------------------------------------------- 指纹信号


class TestFingerprintSignals:
    def test_is_fingerprint_detection(self):
        assert is_fingerprint({"text": {"must": ["a"]}})
        assert is_fingerprint({"anchors": []})
        assert is_fingerprint({"table_headers": ["金额"]})
        assert is_fingerprint({"page_size": [595, 842]})
        assert not is_fingerprint({"must": ["a"], "any": ["b"]})  # 老写法

    def test_anchor_match_with_area(self):
        fp = _fp([Word(330, 81, 395, 91, "发票号码")])  # 右上角

        assert anchor_match(fp, {"text": "发票号码", "area": "top_right"})
        assert not anchor_match(fp, {"text": "发票号码", "area": "top_left"})
        assert anchor_match(fp, {"text": "发票号码"})  # 无 area：命中即可
        assert not anchor_match(fp, {"text": "不存在的词"})

    def test_area_left_right_semantics(self):
        left_word = Word(60, 300, 100, 310, "建筑服务发生地")
        right_word = Word(400, 300, 460, 310, "价税合计")

        assert anchor_match(_fp([left_word]), {"text": "建筑服务发生地", "area": "left"})
        assert not anchor_match(_fp([left_word]), {"text": "建筑服务发生地", "area": "right"})
        assert anchor_match(_fp([right_word]), {"text": "价税合计", "area": "right"})

    def test_header_match_ignores_spaces(self):
        fp = _fp([Word(510, 251, 536, 260, "税 额")])

        assert header_match(fp, "税额")
        assert header_match(fp, "税 额")
        assert not header_match(fp, "金额")

    def test_fingerprint_full_score(self):
        """文本+锚点+表头+页面全部命中 → 100 分（方案 §32 评分示例）。"""
        words = [
            Word(330, 81, 395, 91, "发票号码"),        # top_right
            Word(60, 300, 130, 310, "建筑服务发生地"),  # left
            Word(60, 330, 120, 340, "建筑项目名称"),    # left
            Word(60, 251, 96, 260, "项目名称"),
            Word(230, 251, 248, 260, "数量"),
            Word(350, 251, 368, 260, "金额"),
            Word(420, 251, 474, 260, "税率/征收率"),
            Word(500, 251, 518, 260, "税额"),
        ]
        identify = {
            "text": {"must": ["建筑服务发生地", "建筑项目名称"], "any": ["发票号码"]},
            "anchors": [
                {"text": "发票号码", "area": "top_right"},
                {"text": "建筑服务发生地", "area": "left"},
            ],
            "table_headers": ["项目名称", "数量", "金额", "税率/征收率", "税额"],
            "page_size": [PAGE_W, PAGE_H],
        }

        score, report = evaluate_fingerprint(_fp(words), identify)

        assert score == pytest.approx(100.0)
        assert [part["part"] for part in report["parts"]] == ["text", "anchors", "table", "page"]

    def test_fingerprint_min_score_and_exclude(self):
        words = [Word(60, 251, 96, 260, "项目名称")]
        identify = {"text": {"any": ["项目名称"]}, "min_score": 60}

        # 单一信号命中 → 按配置权重归一化后满分
        score, _ = evaluate_fingerprint(_fp(words), identify)
        assert score == pytest.approx(100.0)

        # exclude 命中 → 直接淘汰
        excluded = {**identify, "text": {"any": ["项目名称"], "exclude": ["项目名称"]}}
        assert evaluate_fingerprint(_fp(words), excluded) is None

    def test_old_style_scoring_unchanged(self):
        """老写法评分公式不变（既有测试语义兼容）。"""
        template = {
            "identify": {
                "priority": 100,
                "must": ["建筑服务发生地", "建筑项目名称"],
                "any": ["电子发票", "增值税专用发票"],
                "min_score": 20,
            }
        }
        text = "电子发票（增值税专用发票）  建筑服务发生地  建筑项目名称"
        # must 2×10 + any 2×2 + priority 100×0.001 = 24.1（与一期方案一致）
        assert evaluate_template(text, template) == pytest.approx(24.1)
        assert evaluate_template("建筑服务发生地", template) is None


# ----------------------------------------------------------- 识别集成


class TestIdentifyIntegration:
    def test_construction_invoice_matches_v1_with_variant(self, tmp_path):
        from tests.test_invoice_multiline import _make_invoice

        pdf = tmp_path / "v1.pdf"
        _make_invoice(pdf, rows=2)

        engine = TemplateEngine(TEMPLATES_DIR)
        result = engine.identify(str(pdf))

        assert result.mode == "match"
        assert result.template_id == "invoice_v1"
        assert result.score > 90  # 指纹近乎全命中
        assert result.variant == "construction"  # 建筑服务变体
        assert result.fingerprint and result.fingerprint["parts"]
        # variant 应用：建筑服务两项转必填（optional 已移除）
        for name in ("construction_site", "project_name"):
            assert "optional" not in result.template["fields"][name]

    def test_variant_invoice_matches_v2_without_construction(self, tmp_path):
        from tests.test_invoice_variant_columns import _make_variant_invoice

        pdf = tmp_path / "v2.pdf"
        _make_variant_invoice(pdf, with_construction_block=False, rows=1)

        engine = TemplateEngine(TEMPLATES_DIR)
        result = engine.identify(str(pdf))

        assert result.mode == "match"
        assert result.template_id == "invoice_v2"
        assert result.variant is None
        # 无变体：建筑服务两项保持 base 的 optional
        assert result.template["fields"]["construction_site"].get("optional") is True

    def test_legacy_invoice_falls_back(self, tmp_path):
        from tests.test_generic_fallback import _make_legacy_invoice

        pdf = tmp_path / "legacy.pdf"
        _make_legacy_invoice(pdf)

        result = TemplateEngine(TEMPLATES_DIR).identify(str(pdf))

        assert result.mode == "fallback"
        assert result.template_id == "invoice_generic"


# ----------------------------------------------------------- Base Schema + Variant


class TestBaseSchemaAndVariant:
    def test_extends_merges_fields_and_shared_config(self):
        engine = TemplateEngine(TEMPLATES_DIR)
        tpl = engine.get("invoice_v2")

        # 字段：模板只写定位键，类型/严重程度继承 base
        assert tpl["fields"]["invoice_no"]["critical"] is True
        assert tpl["fields"]["invoice_no"]["pattern"] == "^\\d{8,20}$"
        assert tpl["fields"]["spec_model"]["optional"] is True
        # 公共配置：regions/table/structure/business_check 继承 base
        assert tpl["regions"]["totals"]["start_anchor"] == ["合 计", "合计"]
        assert tpl["table"]["required_columns"] == ["name", "amount", "tax"]
        assert tpl["business_check"]["totals"]["grand"] == "total_amount"
        assert tpl["business_rules"]

    def test_prepare_applies_variant_with_text(self):
        engine = TemplateEngine(TEMPLATES_DIR)
        text = "电子发票 建筑服务发生地 建筑项目名称"

        variant, prepared = engine.prepare(engine.get("invoice_v2"), text)

        assert variant == "construction"
        assert "optional" not in prepared["fields"]["construction_site"]

    def test_prepare_without_text_skips_variant(self):
        engine = TemplateEngine(TEMPLATES_DIR)

        variant, prepared = engine.prepare(engine.get("invoice_v2"))

        assert variant is None
        assert prepared["fields"]["construction_site"].get("optional") is True

    def test_prepare_merges_base_for_manual_templates(self):
        """显式传入的模板与自动识别同构：base 合并后字段才有类型/严重程度。"""
        engine = TemplateEngine(TEMPLATES_DIR)
        manual = {
            "template": "invoice_v2",
            "mode": "dynamic",
            "extends": "invoice",
            "fields": {
                "invoice_no": {"page": 0, "anchor": "发票号码", "direction": "right"},
            },
        }

        _, prepared = engine.prepare(manual)

        assert prepared["fields"]["invoice_no"]["critical"] is True
        assert prepared["fields"]["invoice_no"]["pattern"] == "^\\d{8,20}$"
