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

# 锚点与右侧值词的水平容差（pt）：发票等版式的值词 x0 会比标签 x1 略微
# 左移 1~5pt（字距挤压），严格 >= 会漏掉所有候选，故允许少量重叠。
_X_TOLERANCE = 6.0

# 候选与锚点的行/列重叠质量阈值：≥ 该比例视为"严格同行/同列"。
# 擦边重叠（如金额列的值与"税率/征收率"表头仅擦边几 pt）会被降到宽松层，
# 避免多个锚点共存时借用相邻列的候选抢先命中（"税"→取到金额列的值）；
# 宽松层仍作为兜底保留，只有严格层完全无匹配时才使用。
_STRICT_OVERLAP_RATIO = 0.5

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


def _overlap_ratio(a: _Word, b: _Word, axis: str) -> float:
    """候选与锚点在指定轴上的重叠质量 = 重叠长度 / 较短一方长度。

    axis="x" 用于 below（同列判断），axis="y" 用于 right（同行判断）。
    完全同词/包含关系为 1.0；擦边重叠接近 0。
    """
    if axis == "x":
        lo, hi = max(a.x0, b.x0), min(a.x1, b.x1)
        span = min(a.x1 - a.x0, b.x1 - b.x0)
    else:
        lo, hi = max(a.y0, b.y0), min(a.y1, b.y1)
        span = min(a.y1 - a.y0, b.y1 - b.y0)
    if span <= 0:
        return 0.0
    return max(0.0, min(1.0, (hi - lo) / span))


def extract_anchor_field(
    name: str,
    words: list[_Word],
    spec: dict[str, Any],
    parser_name: str = "pymupdf-dynamic",
    anchor_words: list[_Word] | None = None,
) -> FieldResult:
    """按 anchor/direction 规则从给定词列表提取字段。

    DynamicRegionParser（PyMuPDF 取词）与 PdfplumberValidator（pdfplumber
    独立切词交叉验证）共用的唯一实现，保证两边规则完全一致。

    anchor_words：可选的"找锚点专用"词表。pdfplumber 交叉验证场景下标签与
    值的字距差异大：宽松切词才能合成完整标签词、保守切词才能保证值不跨列
    粘连，故用宽松词表定位锚点、保守词表提取值。

    候选分层遍历：与锚点严格同行/同列的候选优先，擦边候选兜底——避免
    "税"这类多命中锚点借用相邻列的候选抢先命中（取错值）。
    pick="last" 时在同一层内取最后一个匹配（如"金额"列最下方的合计值）。
    """
    anchor = spec.get("anchor")
    if not anchor:
        result = FieldResult(name, "", parser=parser_name)
        result.fail("动态规则缺少 anchor 配置")
        return result

    direction = spec.get("direction", "right")
    pattern = spec.get("pattern")
    pick = spec.get("pick", "first")
    groups = _anchor_candidate_groups(words, anchor, direction, spec, anchor_words)

    for strict in (True, False):
        matches: list[_Word] = []
        for _anchor_word, candidates in groups:
            for quality, value_word in candidates:
                if (quality >= _STRICT_OVERLAP_RATIO) != strict:
                    continue  # 本轮只处理对应层级的候选
                value = _strip_leading_separators(value_word.text)
                if pattern and not re.fullmatch(pattern, value):
                    continue  # 该候选不合法，继续找下一个
                matches.append(value_word)
        if matches:
            chosen = matches[-1] if pick == "last" else matches[0]
            return _build_field_result(name, chosen, spec, parser_name)

    result = FieldResult(name, "", parser=parser_name)
    if any(candidates for _, candidates in groups):
        result.fail(f"锚点 {anchor!r} 附近的候选值均不匹配 pattern {pattern}")
    else:
        result.fail(f"未找到锚点 {anchor!r} 或锚点附近没有候选值")
    return result


def _build_field_result(
    name: str, value_word: _Word, spec: dict[str, Any], parser_name: str
) -> FieldResult:
    """把候选词归一化 + 校验后包装成 FieldResult。"""
    value = _strip_leading_separators(value_word.text)
    normalizer = _NORMALIZERS.get(spec.get("type", "string"), normalize_text)
    normalized = normalizer(value) if value else None
    if isinstance(normalized, Decimal):
        normalized = format(normalized, "f")
    result = FieldResult(
        field_name=name,
        raw_value=value,
        normalized_value=normalized,
        parser=parser_name,
    )
    return validate_field(result, spec)


def _anchor_candidate_groups(
    words: list[_Word],
    anchor: str,
    direction: str,
    spec: dict[str, Any],
    anchor_words: list[_Word] | None = None,
) -> list[tuple[_Word, list[tuple[float, _Word]]]]:
    """返回 [(锚点词, [(重叠质量, 候选词), ...]), ...]；无锚点时为 []。

    锚点优先从 anchor_words（宽松切词）查找，缺失时回退到 words。
    重叠质量用于分层遍历：1.0 = 同词；接近 1 = 严格同行/同列；接近 0 = 擦边。
    """
    src = anchor_words if anchor_words is not None else words
    anchors = [w for w in src if anchor in w.text]
    if not anchors and anchor_words is not None:
        anchors = [w for w in words if anchor in w.text]
    if not anchors:
        return []

    groups: list[tuple[_Word, list[tuple[float, _Word]]]] = []
    for anchor_word in anchors:
        candidates: list[tuple[float, _Word]] = []
        # 情形 1：值与锚点同词（如 "合同编号：HT20260901"），同词视为最高质量
        tail = anchor_word.text.split(anchor, 1)[1]
        tail = _strip_leading_separators(tail)
        if tail:
            candidates.append(
                (1.0, _Word(anchor_word.x0, anchor_word.y0, anchor_word.x1, anchor_word.y1, tail))
            )

        # 情形 2：同行右侧最近的独立词（pattern 逐词/拼接尝试）
        if direction == "right":
            same_line_right = [
                w for w in words
                if w is not anchor_word
                and w.x0 >= anchor_word.x1 - _X_TOLERANCE
                and (not spec.get("same_line", True) or _line_overlap(anchor_word, w))
            ]
            same_line_right.sort(key=lambda w: w.x0)
            for w in _joined_candidates(same_line_right, limit=3):
                candidates.append((_overlap_ratio(anchor_word, w, "y"), w))

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
            below_mode = spec.get("below_mode", "prefix")
            produced: list[_Word] = []
            if below_mode == "merge":
                # 跨行单元格（如发票表格值被拆成两行）：全部按序合并为完整值
                if below:
                    merged = "".join(w.text for w in below)
                    produced.append(
                        _Word(below[0].x0, below[0].y0, below[-1].x1, below[-1].y1, merged)
                    )
            elif below_mode == "each":
                # 逐词候选（配合 pattern 区分同列多行，如明细金额 vs 合计金额）
                produced.extend(below)
            else:
                produced.extend(_joined_candidates(below, limit=3))
            for w in produced:
                candidates.append((_overlap_ratio(anchor_word, w, "x"), w))

        groups.append((anchor_word, _filter_x_range(candidates, spec)))
    return groups


def _filter_x_range(
    candidates: list[tuple[float, _Word]], spec: dict[str, Any]
) -> list[tuple[float, _Word]]:
    """按模板的 x_min / x_max 过滤候选（以候选起始 x0 为准）。"""
    x_min = spec.get("x_min")
    x_max = spec.get("x_max")
    if x_min is None and x_max is None:
        return candidates

    def in_range(c: _Word) -> bool:
        if x_min is not None and c.x0 < float(x_min):
            return False
        if x_max is not None and c.x0 > float(x_max):
            return False
        return True

    return [(q, c) for q, c in candidates if in_range(c)]


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


class DynamicRegionParser:
    """按模板 JSON 的 anchor/direction 规则提取字段。

    字段规则键（除固定模式的 page/rect 外新增）：
      anchor        锚点关键词（word 包含即命中）
      direction     right = 同行右侧；below = 下方
      same_line     right 方向是否要求同行（默认 true）
      max_distance  below 方向的最大垂直距离（默认 50）
      below_mode    below 取词方式：
                    prefix（默认）= 前 1/前 2/前 3 词前缀拼接候选；
                    merge = 范围内所有词按序合并为一个候选（跨行单元格完整值）；
                    each  = 范围内每个词各自成候选（配合 pattern 精确挑选）
      pattern       值的正则（fullmatch）；不匹配的候选会被跳过
      pick          first（默认）= 取首个匹配；last = 取最后一个匹配
                    （如合计行在明细下方时，取同列最下方的值）
      x_min / x_max 候选值词起始 x 的范围（pt），用于区分左右分栏
                    （如发票购方/销方栏的"名称："锚点文字完全相同）

    候选按"与锚点的行/列重叠质量"分层：先严格同行/同列，再擦边候选兜底，
    避免"税"这类多命中锚点（纳税人识别号、税率、税额…）借用相邻列的候选。
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
                report.fields[name] = extract_anchor_field(name, words, spec)
        report.apply_business_rules(template.get("business_rules"))
        return report
