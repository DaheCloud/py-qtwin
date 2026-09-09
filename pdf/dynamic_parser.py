"""动态区域解析器：锚点 + 相对方向 + 距离/正则定位（文档 §23.2）。

与固定区域解析器解析同一批字段，区别只在定位策略：
  固定模式：page + rect 直接取词
  动态模式：get_text("words") 全页取词 → 找锚点词 → 按规则找值

实测行为（china-s 字体）：
  "合同编号：HT20260901" 会作为一个 word 返回，
  因此锚点匹配用"包含"语义，值可以与锚点同词（冒号后），
  也可以是同行右侧 / 下方的独立 word。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pymupdf

from pdf.normalizers import normalize_amount, normalize_date, normalize_text
from pdf.pymupdf_parser import ParseReport
from pdf.validators import FieldResult, validate_field

# 锚点与值之间的分隔符（同词情形）：全角/半角冒号、空格
_SEPARATORS = "：: \u3000"

_NORMALIZERS: dict[str, Any] = {
    "string": normalize_text,
    "decimal": normalize_amount,
    "date": normalize_date,
}


@dataclass(frozen=True)
class _Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str


def _page_words(page: pymupdf.Page) -> list[_Word]:
    return [
        _Word(w[0], w[1], w[2], w[3], w[4])
        for w in page.get_text("words")
    ]


def _strip_leading_separators(text: str) -> str:
    return text.lstrip(_SEPARATORS)


def _line_overlap(a: _Word, b: _Word) -> bool:
    """两词在垂直方向上重叠，视为同一行。"""
    return b.y0 < a.y1 and b.y1 > a.y0


def _x_overlap(a: _Word, b: _Word) -> bool:
    return b.x0 < a.x1 and b.x1 > a.x0


class DynamicRegionParser:
    """按模板 JSON 的 anchor/direction 规则提取字段。

    字段规则键（除固定模式的 page/rect 外新增）：
      anchor        锚点关键词（word 包含即命中）
      direction     right = 同行右侧；below = 下方
      same_line     right 方向是否要求同行（默认 true）
      max_distance  below 方向的最大垂直距离（默认 50）
      pattern       值的正则（fullmatch）；不匹配的候选会被跳过
    """

    def parse(self, pdf_path: str, template: dict[str, Any]) -> ParseReport:
        mode = template.get("mode", "fixed")
        if mode != "dynamic":
            raise ValueError(f"DynamicRegionParser 仅支持 dynamic 模板，收到 mode={mode!r}")

        report = ParseReport(template_id=template.get("template", "unknown"), mode=mode)
        with pymupdf.open(pdf_path) as doc:
            for name, spec in template.get("fields", {}).items():
                page_no = int(spec.get("page", 0))
                if page_no >= len(doc):
                    result = FieldResult(name, "", parser="pymupdf-dynamic")
                    result.fail(f"页码 {page_no} 超出文档范围（共 {len(doc)} 页）")
                    report.fields[name] = result
                    continue

                words = _page_words(doc[page_no])
                result = self._extract_field(name, words, spec)
                report.fields[name] = result
        report.apply_business_rules(template.get("business_rules"))
        return report

    # ------------------------------------------------------------ 字段级

    def _extract_field(self, name: str, words: list[_Word], spec: dict[str, Any]) -> FieldResult:
        anchor = spec.get("anchor")
        if not anchor:
            result = FieldResult(name, "", parser="pymupdf-dynamic")
            result.fail("动态规则缺少 anchor 配置")
            return result

        direction = spec.get("direction", "right")
        pattern = spec.get("pattern")
        candidates = self._find_candidates(words, anchor, direction, spec)

        for value_word in candidates:
            value = _strip_leading_separators(value_word.text)
            if pattern and not re.fullmatch(pattern, value):
                continue  # 该候选不合法，继续找下一个
            normalizer = _NORMALIZERS.get(spec.get("type", "string"), normalize_text)
            normalized = normalizer(value) if value else None
            if isinstance(normalized, Decimal):
                normalized = format(normalized, "f")
            result = FieldResult(
                field_name=name,
                raw_value=value,
                normalized_value=normalized,
                parser="pymupdf-dynamic",
            )
            return validate_field(result, spec)

        result = FieldResult(name, "", parser="pymupdf-dynamic")
        if not candidates:
            result.fail(f"未找到锚点 {anchor!r} 或锚点附近没有候选值")
        else:
            result.fail(f"锚点 {anchor!r} 附近的候选值均不匹配 pattern {pattern}")
        return result

    def _find_candidates(self, words: list[_Word], anchor: str, direction: str, spec: dict[str, Any]) -> list[_Word]:
        """返回按优先级排序的候选值词列表（可能为空）。"""
        anchors = [w for w in words if anchor in w.text]
        if not anchors:
            return []

        candidates: list[_Word] = []
        for anchor_word in anchors:
            # 情形 1：值与锚点同词（如 "合同编号：HT20260901"）
            tail = anchor_word.text.split(anchor, 1)[1]
            tail = _strip_leading_separators(tail)
            if tail:
                candidates.append(_Word(anchor_word.x0, anchor_word.y0, anchor_word.x1, anchor_word.y1, tail))

            # 情形 2：同行右侧最近的独立词（pattern 逐词/拼接尝试）
            if direction == "right":
                same_line_right = [
                    w for w in words
                    if w is not anchor_word
                    and w.x0 >= anchor_word.x1
                    and (not spec.get("same_line", True) or _line_overlap(anchor_word, w))
                ]
                same_line_right.sort(key=lambda w: w.x0)
                candidates.extend(self._joined_candidates(same_line_right, limit=3))

            # 情形 3：下方最近词（x 有重叠，y 距离受限）
            elif direction == "below":
                max_distance = float(spec.get("max_distance", 50))
                below = [
                    w for w in words
                    if w is not anchor_word
                    and w.y0 >= anchor_word.y1
                    and (w.y0 - anchor_word.y1) <= max_distance
                    and _x_overlap(anchor_word, w)
                ]
                below.sort(key=lambda w: w.y0)
                candidates.extend(self._joined_candidates(below, limit=3))

        return candidates

    @staticmethod
    def _joined_candidates(sorted_words: list[_Word], limit: int = 3) -> list[_Word]:
        """把邻近的词按序拼接成候选（处理值被切成多个 word 的情况）。

        产出：第 1 个词、前 2 词拼接、前 3 词拼接……最多 limit 个候选。
        """
        out: list[_Word] = []
        for i in range(min(limit, len(sorted_words))):
            part = sorted_words[: i + 1]
            text = "".join(w.text for w in part)
            out.append(_Word(part[0].x0, part[0].y0, part[-1].x1, part[-1].y1, text))
        return out
