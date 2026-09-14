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
from decimal import Decimal
from typing import Any

import pymupdf

from pdf.normalizers import normalize_amount, normalize_date, normalize_text
from pdf.policies import (
    FALLBACK_DOCUMENT,
    REGION_DOCUMENT,
    REGION_FAIL,
    REGION_PAGE,
    field_fallback_policy,
    field_region_policy,
)
from pdf.pymupdf_parser import ParseReport
from pdf.region_engine import field_words
from pdf.table_engine import apply_to_report, extract_table_multipage
from pdf.validators import FieldResult, apply_optional, validate_field
from pdf.words import (
    ANCHOR_MERGE_GAP_RATIO as _ANCHOR_MERGE_GAP_RATIO,
    Word as _Word,
    clean_value_text as _clean_value_text,
    count_rows as _count_rows,
    join_by_row as _join_by_row,
    line_overlap as _line_overlap,
    merged_line_words as _merged_line_words,
    merged_neighbor_words as _merged_neighbor_words,
    merged_page_words,
    page_words as _page_words,
    with_number_fragments as _with_number_fragments,
    x_overlap as _x_overlap,
)

# 锚点与右侧值词的水平容差（pt）：发票等版式的值词 x0 会比标签 x1 略微
# 左移 1~5pt（字距挤压），严格 >= 会漏掉所有候选，故允许少量重叠。
_X_TOLERANCE = 6.0

# 候选与锚点的行/列重叠质量阈值：≥ 该比例视为"严格同行/同列"。
# 擦边重叠（如金额列的值与"税率/征收率"表头仅擦边几 pt）会被降到宽松层，
# 避免多个锚点共存时借用相邻列的候选抢先命中（"税"→取到金额列的值）；
# 宽松层仍作为兜底保留，只有严格层完全无匹配时才使用。
# below（同列）的贴合度按"列窗口覆盖度"算（见 _column_quality），不是按
# 锚点标签自身的宽度——单字标签（"金" = 9pt）会把擦边几 pt 的邻行值算成高贴合。
_STRICT_OVERLAP_RATIO = 0.5

_NORMALIZERS: dict[str, Any] = {
    "string": normalize_text,
    "decimal": normalize_amount,
    "date": normalize_date,
}


# 文本层可能带字间距空格（"118812. 57"、"金 额"）：pattern 校验忽略空白，
# 否则金额这类正则会被空格判为不匹配。取值本身仍保留原文（raw_value）。
_PATTERN_IGNORED_CHARS = str.maketrans("", "", " \u3000\t")


def _anchor_window(anchor_word: _Word, words: list[_Word], spec: dict[str, Any]) -> _Word:
    """锚点的 x 窗口；anchor_span=true 时向右延伸到本列的右边界（相邻列表头起点）。

    发票表格里同一列的值常靠右对齐，起点会超出表头文字的范围（"金额"表头只有
    两个字宽，值却落在其右侧）；而两字表头又常被字间距切成单字（"金" + "额"）。
    若直接取"紧邻下一个词的起点"，遇到被切开的表头就会停在"额"处，整列靠右的
    值全部漏采（典型症状：候选值只剩恰好压在表头下方的"（小写）"）。

    因此逐词向右推进：单字碎片且"它到下一个词的间距 > 它到锚点的间距"时，
    判定为本表头的字间距碎片、继续推进；否则该词就是相邻列表头，取其起点为列
    右边界。只推进单字碎片，避免窗口蔓延进相邻列。
    """
    if not spec.get("anchor_span"):
        return anchor_word
    following = sorted(
        (
            w
            for w in words
            if w is not anchor_word
            and _line_overlap(anchor_word, w)
            and w.x0 >= anchor_word.x1 - _X_TOLERANCE
        ),
        key=lambda w: w.x0,
    )
    if not following:
        return anchor_word

    right = anchor_word.x1
    for i, token in enumerate(following):
        nxt = following[i + 1] if i + 1 < len(following) else None
        if len(token.text) == 1:
            if nxt is None:
                right = token.x1  # 行尾单字：同属本表头（无相邻列可比）
                break
            if (nxt.x0 - token.x1) > (token.x0 - right):
                right = token.x1  # 词间距更大 → 该单字是本表头的字间距碎片
                continue
        right = token.x0  # 相邻列表头起点 → 列右边界
        break
    return _Word(anchor_word.x0, anchor_word.y0, max(anchor_word.x1, right), anchor_word.y1, anchor_word.text)


def _stop_boundary_y(words: list[_Word], anchor_word: _Word, spec: dict[str, Any]) -> float | None:
    """stop_anchor 关键词命中处的最小 y（截断线）；未配置或无命中返回 None。

    任意列命中即算（如"合 计"标签在项目名称列、金额在金额列），带字间距的
    标签同样通过相邻词拼接匹配；用于行数统计时排除合计行/建筑服务信息块。
    """
    keywords = spec.get("stop_anchor") or []
    if not keywords:
        return None
    max_distance = float(spec.get("max_distance", 800))
    below_any = [
        w for w in words
        if w.y0 >= anchor_word.y1 and (w.y0 - anchor_word.y1) <= max_distance
    ]
    boundaries = [w.y0 for w in below_any if any(kw in w.text for kw in keywords)]
    boundaries.extend(
        w.y0 for w in _merged_neighbor_words(below_any) if any(kw in w.text for kw in keywords)
    )
    # 整行拼接兜底：实票标签按字间距排版（"合 计" → "合"+"计"，间距可达 30pt+），
    # 相邻词拼接（间距上限 1.2 字高）兜不住，整行拼接后才能命中"合计"/"合 计"。
    boundaries.extend(
        w.y0 for w in _merged_line_words(below_any) if any(kw in w.text for kw in keywords)
    )
    return min(boundaries) if boundaries else None


def _scope_line_y(
    words: list[_Word], keywords: list[str] | None, *, below: bool
) -> float | None:
    """范围标签所在行的 y 边界：below=True 取最靠上命中的行底，否则取最靠下命中的行顶。"""
    if not keywords:
        return None
    hits = [w for w in words if any(kw in w.text for kw in keywords)]
    if not hits:
        hits = [
            w for w in _merged_line_words(words) if any(kw in w.text for kw in keywords)
        ]
    if not hits:
        return None
    return max(w.y1 for w in hits) if below else min(w.y0 for w in hits)


def _resolve_scope(words: list[_Word], spec: dict[str, Any]) -> list[_Word]:
    """按 scope.start_anchor / scope.end_anchor 限定"取值搜索区"（方案 §5）。

    发票里"金额/税额/名称/日期"常在多处重复，范围限定能显著降低锚点误匹配；
    边界用"行"判断（垂直重叠），所以与范围标签同行的值不会被误伤。
    所有范围标签都不存在时退回全页，避免规则因缺标签而整体失效。
    """
    scope = spec.get("scope")
    if not scope:
        return words
    start_y = _scope_line_y(words, scope.get("start_anchor"), below=True)
    end_y = _scope_line_y(words, scope.get("end_anchor"), below=False)
    if start_y is None and end_y is None:
        return words
    return [
        w
        for w in words
        if (start_y is None or w.y1 > start_y) and (end_y is None or w.y0 < end_y)
    ]


def _column_quality(window: _Word, w: _Word) -> float:
    """候选与锚点所在列的贴合度 = 候选落在列窗口内的覆盖比例（0~1）。

    分母取"列窗口宽度"与"候选宽度"的较小者：窗口更宽时看候选有多少落在列内
    （左对齐/居中值），候选更宽时看它覆盖了窗口多少（超出列宽的长值）。

    不用"与锚点标签的重叠 ÷ 标签宽度"：单字标签"金"只有 9pt 宽，下方邻行的
    "价税合计"值（x0=440.8）只擦到窗口右缘 5.7pt，按标签宽算却是 0.63，
    会被误判为严格同列，pick=last 就会把价税合计（129505.70）当成金额合计。
    """
    overlap = min(window.x1, w.x1) - max(window.x0, w.x0)
    span = min(window.x1 - window.x0, w.x1 - w.x0)
    if overlap <= 0 or span <= 0:
        return 0.0
    return max(0.0, min(1.0, overlap / span))


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
    page: int | None = None,
) -> FieldResult:
    """按 anchor/direction 规则从给定词列表提取字段。

    anchor 支持两种写法：
      字符串     —— 单一锚点；
      字符串列表 —— 候选锚点按序尝试，首个提取成功即返回（通用兜底模板
                    用于兼容不同版式的标签，如 "项目名称"/"货物或应税劳务"）。

    DynamicRegionParser（PyMuPDF 取词）与 PdfplumberValidator（pdfplumber
    独立切词交叉验证）共用的唯一实现，保证两边规则完全一致。

    anchor_words：可选的"找锚点专用"词表。pdfplumber 交叉验证场景下标签与
    值的字距差异大：宽松切词才能合成完整标签词、保守切词才能保证值不跨列
    粘连，故用宽松词表定位锚点、保守词表提取值。

    候选分层遍历：与锚点严格同行/同列的候选优先，擦边候选兜底——避免
    "税"这类多命中锚点借用相邻列的候选抢先命中（取错值）。
    pick="last" 时在同一层内取最后一个匹配（如"金额"列最下方的合计值）。

    spec["optional"]=true 时，未提取到值不算失败（通用模板的可选字段）。
    """
    anchor = spec.get("anchor")
    if isinstance(anchor, (list, tuple)):
        return _extract_first_anchor(
            name, words, spec, parser_name, anchor_words, list(anchor), page
        )
    return _extract_single_anchor(name, words, spec, parser_name, anchor_words, page)


def _extract_first_anchor(
    name: str,
    words: list[_Word],
    spec: dict[str, Any],
    parser_name: str,
    anchor_words: list[_Word] | None,
    anchors: list[str],
    page: int | None = None,
) -> FieldResult:
    """候选锚点按序尝试：任一取到值即返回，全部失败汇总为一个失败结果。

    判据用"valid 且有值"而非仅 valid：optional 字段失败时会被转成
    "合法空值"（valid 但无值），只判 valid 会让第一个候选锚点失败后
    就提前返回，后面的候选锚点失去机会。
    """
    last_error = ""
    for anchor in anchors:
        result = _extract_single_anchor(
            name, words, {**spec, "anchor": anchor}, parser_name, anchor_words, page
        )
        if result.valid and result.normalized_value:
            return result
        last_error = result.errors[-1] if result.errors else ""
    result = FieldResult(name, "", parser=parser_name, page=page, region=spec.get("region"))
    result.anchor_hit = False
    suffix = f"（最后错误：{last_error}）" if last_error else ""
    result.fail(f"候选锚点 {anchors} 均未提取成功{suffix}")
    return apply_optional(result, spec)


def _extract_single_anchor(
    name: str,
    words: list[_Word],
    spec: dict[str, Any],
    parser_name: str,
    anchor_words: list[_Word] | None,
    page: int | None = None,
) -> FieldResult:
    """单一锚点的提取实现（供 extract_anchor_field 调用）。"""
    anchor = spec.get("anchor")
    if not anchor:
        result = FieldResult(name, "", parser=parser_name, page=page)
        result.anchor_hit = False
        result.fail("动态规则缺少 anchor 配置")
        return result

    direction = spec.get("direction", "right")
    pattern = spec.get("pattern")
    pick = spec.get("pick", "first")
    scoped = _resolve_scope(words, spec)
    groups = _anchor_candidate_groups(
        words, anchor, direction, spec, anchor_words, candidate_words=scoped
    )

    for strict in (True, False):
        matches: list[_Word] = []
        for _anchor_word, candidates in groups:
            for quality, value_word in candidates:
                if (quality >= _STRICT_OVERLAP_RATIO) != strict:
                    continue  # 本轮只处理对应层级的候选
                value = _clean_value_text(value_word.text)
                if pattern and not re.fullmatch(pattern, value.translate(_PATTERN_IGNORED_CHARS)):
                    continue  # 该候选不合法，继续找下一个
                matches.append(value_word)
        if matches:
            chosen = matches[-1] if pick == "last" else matches[0]
            return _build_field_result(
                name, chosen, spec, parser_name, page=page, candidate_count=len(matches)
            )

    result = FieldResult(name, "", parser=parser_name, page=page, region=spec.get("region"))
    # 锚点本身是否存在：命中锚点但值不合法 ≠ 锚点未命中，两者证据含义不同
    result.anchor_hit = bool(groups)
    seen: list[str] = []
    for _anchor_word, candidates in groups:
        for _quality, word in candidates:
            cleaned = _clean_value_text(word.text)
            if cleaned:
                seen.append(cleaned)
    if seen:
        # 带上实际候选值，便于直接判断是 pattern 太严还是取到了邻列内容
        unique = list(dict.fromkeys(seen))
        preview = "、".join(unique[:3]) + ("…" if len(unique) > 3 else "")
        result.fail(f"锚点 {anchor!r} 附近的候选值（{preview}）均不匹配 pattern {pattern}")
    else:
        result.fail(f"未找到锚点 {anchor!r} 或锚点附近没有候选值")
    return apply_optional(result, spec)


def _build_field_result(
    name: str,
    value_word: _Word,
    spec: dict[str, Any],
    parser_name: str,
    *,
    page: int | None = None,
    candidate_count: int = 0,
) -> FieldResult:
    """把候选词归一化 + 校验后包装成 FieldResult（含 V2 取值溯源）。"""
    value = _clean_value_text(value_word.text)
    normalizer = _NORMALIZERS.get(spec.get("type", "string"), normalize_text)
    normalized = normalizer(value) if value else None
    if isinstance(normalized, Decimal):
        normalized = format(normalized, "f")
    result = FieldResult(
        field_name=name,
        raw_value=value,
        normalized_value=normalized,
        parser=parser_name,
        page=page,
        rect=(value_word.x0, value_word.y0, value_word.x1, value_word.y1),
        anchor_hit=True,
        region=spec.get("region"),
        candidate_count=candidate_count,
    )
    return validate_field(result, spec)


def _anchor_candidate_groups(
    words: list[_Word],
    anchor: str,
    direction: str,
    spec: dict[str, Any],
    anchor_words: list[_Word] | None = None,
    candidate_words: list[_Word] | None = None,
) -> list[tuple[_Word, list[tuple[float, _Word]]]]:
    """返回 [(锚点词, [(重叠质量, 候选词), ...]), ...]；无锚点时为 []。

    锚点优先从 anchor_words（宽松切词）查找，缺失时回退到 words。
    candidate_words 为 scope 过滤后的候选词表（缺省同 words）：锚点始终在全页
    词表里找，只有"取值"受限——表头行常同时放着范围标签与本字段锚点
    （"项目名称"与"金 额"同一行），连锚点一起过滤会让规则完全失效。
    重叠质量用于分层遍历：1.0 = 同词；接近 1 = 严格同行/同列；接近 0 = 擦边。
    """
    cand = candidate_words if candidate_words is not None else words
    src = anchor_words if anchor_words is not None else words
    anchors = [w for w in src if anchor in w.text]
    if not anchors and anchor_words is not None:
        anchors = [w for w in words if anchor in w.text]
    if not anchors:
        # 兜底：字间距把标签切成多个词时（"单 位" → "单"+"位"），用拼接词再找一次
        anchors = [w for w in _merged_neighbor_words(src) if anchor in w.text]
    if not anchors:
        return []

    groups: list[tuple[_Word, list[tuple[float, _Word]]]] = []
    for anchor_word in anchors:
        candidates: list[tuple[float, _Word]] = []
        # 情形 1：值与锚点同词（如 "合同编号：HT20260901"），同词视为最高质量。
        # same_word=false 时禁用（锚点是长标签前缀时，词尾剩余部分仍是标签
        # 而非值，如"货物或应税劳务、服务名称"对锚点"货物或应税劳务"）。
        # count 模式同理跳过：锚点只是定位器，词尾不是计数值。
        if spec.get("same_word", True) and spec.get("below_mode") != "count":
            tail = anchor_word.text.split(anchor, 1)[1]
            tail = _clean_value_text(tail)
            if tail:
                candidates.append(
                    (1.0, _Word(anchor_word.x0, anchor_word.y0, anchor_word.x1, anchor_word.y1, tail))
                )

        # 情形 2：同行右侧最近的独立词（pattern 逐词/拼接尝试）
        if direction == "right":
            same_line_right = [
                w for w in cand
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
            window = _anchor_window(anchor_word, cand, spec)
            below = [
                w for w in cand
                if w is not anchor_word
                and w.y0 >= anchor_word.y1
                and (w.y0 - anchor_word.y1) <= max_distance
                and _x_overlap(window, w)
            ]
            below.sort(key=lambda w: w.y0)
            below_mode = spec.get("below_mode", "prefix")
            produced: list[_Word] = []
            if below_mode == "merge":
                # 跨行单元格（值折成两~三行）：按序合并为完整值。
                # 配了 stop_anchor 时先截断，避免把合计行/下一字段标签并进来
                # （放宽 max_distance 以容纳多行文案时这一步是必需的）。
                cut_y = _stop_boundary_y(words, anchor_word, spec)
                merge_words = [w for w in below if cut_y is None or w.y0 < cut_y]
                if merge_words:
                    merged = "".join(w.text for w in merge_words)
                    produced.append(
                        _Word(
                            merge_words[0].x0,
                            merge_words[0].y0,
                            merge_words[-1].x1,
                            merge_words[-1].y1,
                            merged,
                        )
                    )
            elif below_mode == "each":
                # 逐词候选（配合 pattern 区分同列多行，如明细金额 vs 合计金额）
                produced.extend(below)
            elif below_mode == "row":
                # 按行拼接：同一行的多个词合成一个候选。数值被切词成多段时
                # （"118812." + "57"），右侧碎片可能不落在锚点 x 范围内，需
                # 先按"同行 + 小间距"把数字碎片补齐再拼接。
                # 配了 stop_anchor 时先截断：金额列要取"合 计"行（pick=last），
                # 不能把更下方的"价税合计（小写）¥…"也算进来。
                cut_y = _stop_boundary_y(words, anchor_word, spec)
                row_words = [w for w in below if cut_y is None or w.y0 < cut_y]
                produced.extend(_join_by_row(_with_number_fragments(row_words, cand)))
            elif below_mode == "count":
                # 行数统计（明细行数）：同列向下逐行计数，遇 stop_anchor 截断，
                # 避免把合计行/建筑服务信息块也算成明细行。
                cut_y = _stop_boundary_y(words, anchor_word, spec)
                counted = [w for w in below if cut_y is None or w.y0 < cut_y]
                produced.append(
                    _Word(
                        anchor_word.x0,
                        anchor_word.y0,
                        anchor_word.x1,
                        anchor_word.y1,
                        str(_count_rows(counted)),
                    )
                )
            else:
                produced.extend(_joined_candidates(below, limit=3))
            # 跨列粘连词（相邻单元格文字被切词并成一个 word）会明显越过本列左边界：
            # 候选起点比本列左边界还靠左时判为邻列内容，直接丢弃（宁缺勿错）。
            # 列宽用锚点窗口跨度估算（anchor_span 时含表头标签"金…额"的实际跨度）
            # ——单字锚点"金"自身只有 9pt 宽，拿它当列宽会把靠右对齐的宽数值
            # （"118812.57" x0=393.2，比"金"x0=405.4 还靠左）误判成邻列内容丢弃。
            column_width = max(
                window.x1 - anchor_word.x0,
                anchor_word.x1 - anchor_word.x0,
                1.0,
            )
            column_left = anchor_word.x0 - column_width
            for w in produced:
                if w.x0 < column_left:
                    continue
                candidates.append((_column_quality(window, w), w))

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


def field_page_order(page_no: int, total: int) -> list[int]:
    """字段跨页搜索顺序：配置页优先，其余页按页序兜底（两页发票字段被排到次页）。"""
    if total <= 0 or page_no >= total:
        return []
    return [page_no] + [p for p in range(total) if p != page_no]


# 跨页画布的距离预算增量（pt）：约两页高。画布上"锚点在第 1 页、值在第 2 页
# 中下部"的垂直距离必然 ≥ 一页内容高度，沿用页内 max_distance 会砍掉合法候选。
_CANVAS_DISTANCE_BONUS = 2000.0


def extract_field_cross_page(
    name: str,
    spec: dict[str, Any],
    template: dict[str, Any],
    *,
    page_words_fn,
    total_pages: int,
    anchor_words_fn=None,
    parser_name: str = "pymupdf-dynamic",
) -> FieldResult:
    """跨页字段提取（PyMuPDF 解析器与 pdfplumber 交叉验证共用同一策略）。

    page_words_fn(p) -> list[Word]：第 p 页候选词表（解析器 = PyMuPDF 词表；
    交叉验证 = pdfplumber 保守切词词表）。
    anchor_words_fn(p) -> list[Word]：第 p 页锚点词表（交叉验证用宽松切词
    合成完整标签；缺省与候选词表相同）。

    策略（V2，方案 §3 / §4）：
    1. 配置页优先，失败按页序兜底（字段可能被排到后续页）；
    2. 带 region 的字段**只在区域可解析的页取值**（区域约束始终生效）：
       某页缺少区域锚标签时该页不参与（pick=last 会取到明细行的错值并挡住
       后续页），区域在第 2 页能解析时就正常从第 2 页取——跨页不等于放弃约束；
    3. 所有页都解析不出该区域时，才按 region_failure_policy 决定：
       **fail** = 就地失败（关键字段默认，拒绝退回全页）；
       **page** = 仅配置页退回全页取值；**document** = 用"配置页 + 后续页"
       画布退回全文（跨页）；
    4. 未配置 region 的字段保持原行为：单页失败后走画布兜底。
    """
    page_no = int(spec.get("page", 0))
    fallback_policy = field_fallback_policy(template, spec)
    region_policy = field_region_policy(template, spec)
    # 带 Region 时允许跨页寻找同一个受约束区域，这不是扩大搜索范围；
    # 无 Region 的全文跨页搜索必须显式 opt-in。旧 dynamic_fallback=true
    # 仅为已存在模板保留跨页行为，生产 V2 模板默认 false。
    allow_document = (
        bool(spec.get("region"))
        or fallback_policy == FALLBACK_DOCUMENT
        or bool(template.get("dynamic_fallback", False))
    )
    order = field_page_order(page_no, total_pages) if allow_document else [page_no]
    if page_no < 0 or page_no >= total_pages or not order:
        result = FieldResult(name, "", parser=parser_name)
        result.fail(f"页码 {page_no} 超出文档范围（共 {total_pages} 页）")
        return result

    policy = region_policy
    region_name = spec.get("region")

    first_result: FieldResult | None = None
    region_resolved = False
    for p in order:
        words = page_words_fn(p)
        anchors = anchor_words_fn(p) if anchor_words_fn else None
        scoped = field_words(words, template, spec)
        if region_name and scoped is words:
            # 该页解析不出字段所属区域：本页取值不受区域约束、不可信 → 不用它
            continue
        if region_name:
            region_resolved = True

        if scoped is words:
            result = extract_anchor_field(
                name, words, spec,
                parser_name=parser_name,
                anchor_words=anchors if anchor_words_fn else None,
                page=p,
            )
        else:
            # Region 是主要搜索空间：锚点与候选都先限制在 Region 内；
            # Region 内取不到值时，锚点回退全页查找（交叉验证用宽松词表），
            # 候选仍限制在 Region 内
            result = extract_anchor_field(
                name, scoped, spec, parser_name=parser_name, page=p
            )
            if not (result.valid and result.normalized_value):
                result = extract_anchor_field(
                    name,
                    scoped,
                    spec,
                    parser_name=parser_name,
                    anchor_words=anchors if anchor_words_fn is not None else words,
                    page=p,
                )
            result.region_used = True
        if p == page_no:
            first_result = result
        if result.valid and result.normalized_value:
            if p != page_no and not region_name:
                result.fallback_used = True
            return result

    if region_name and not region_resolved:
        # 各页都没解析出该区域：取值必然不受区域约束 → 由策略决定怎么处理
        if policy == REGION_FAIL:
            # 关键字段默认策略（方案 §4.2）：宁可转人工复核，也不从明细行/
            # 备注区取一个"格式合法但位置错误"的值
            result = FieldResult(
                name, "", parser=parser_name, page=page_no, region=region_name
            )
            result.anchor_hit = False
            result.fail(
                f"区域 {region_name!r} 各页均未解析，region_failure_policy=fail 拒绝退回全页"
            )
            return apply_optional(result, spec)
        if policy == REGION_PAGE:
            # 退回配置页全页取值（page 语义：不跨页，跨页由 document 策略负责）
            words = page_words_fn(page_no)
            anchors = anchor_words_fn(page_no) if anchor_words_fn else None
            result = extract_anchor_field(
                name,
                words,
                spec,
                parser_name=parser_name,
                anchor_words=anchors if anchor_words_fn else None,
                page=page_no,
            )
            result.region_fallback = REGION_PAGE
            result.fallback_used = True
            return result
        # REGION_DOCUMENT：继续走到下方画布兜底（跨页）

    # 全部单页失败：扩展画布（配置页 + 后续页）兜底。
    # 单页文档也走这一步：画布等于该页词表，等价"region 不可解析时退回未约束
    # 提取"（旧行为），否则 region 标签缺失的版式会让字段直接失败。
    canvas = merged_page_words([page_words_fn(p) for p in order])
    canvas_anchors = (
        merged_page_words([anchor_words_fn(p) for p in order]) if anchor_words_fn else None
    )
    # 多页画布才放宽距离预算：单页兜底（region 标签缺失退回未约束提取）必须
    # 保留原来的 max_distance 语义，否则"锚点下方过远"的字段会取到远处错值
    canvas_spec = (
        {**spec, "max_distance": float(spec.get("max_distance", 50)) + _CANVAS_DISTANCE_BONUS}
        if len(order) > 1
        else spec
    )
    scoped = field_words(canvas, template, canvas_spec)
    if scoped is canvas:
        canvas_result = extract_anchor_field(
            name,
            canvas,
            canvas_spec,
            parser_name=parser_name,
            anchor_words=canvas_anchors if anchor_words_fn is not None else None,
        )
    else:
        canvas_result = extract_anchor_field(
            name, scoped, canvas_spec, parser_name=parser_name
        )
        if not (canvas_result.valid and canvas_result.normalized_value):
            canvas_result = extract_anchor_field(
                name,
                scoped,
                canvas_spec,
                parser_name=parser_name,
                anchor_words=(canvas_anchors if anchor_words_fn is not None else canvas),
            )
        canvas_result.region_used = True
    if canvas_result.valid and canvas_result.normalized_value:
        # V2（方案 §4.3）：只有"画布上仍解析不出区域"（scoped is canvas）才是
        # 真正的回退；画布上区域可解析时取值仍受区域约束（region_used=True），
        # 只是跨页取值，不能记成回退扣分。
        if region_name and scoped is canvas:
            # 多页画布 = document（跨页），单页画布 = page（退回本页全页）
            canvas_result.region_fallback = (
                REGION_DOCUMENT if len(order) > 1 else REGION_PAGE
            )
            canvas_result.fallback_used = True
        return canvas_result

    if first_result is not None:
        return first_result
    result = FieldResult(name, "", parser=parser_name, region=region_name)
    result.anchor_hit = False
    result.fail(f"各页均未提取到 {name}（含跨页画布兜底）")
    return result


def _extract_field_across_pages(
    name: str,
    spec: dict[str, Any],
    template: dict[str, Any],
    words_of,
    total_pages: int,
) -> FieldResult:
    """PyMuPDF 解析器的跨页字段提取（词表与锚点词表同源）。"""
    return extract_field_cross_page(
        name, spec, template, page_words_fn=words_of, total_pages=total_pages
    )


class DynamicRegionParser:
    """按模板 JSON 的 anchor/direction 规则提取字段。

    字段规则键（除固定模式的 page/rect 外新增）：
      anchor        锚点关键词（word 包含即命中）；可为字符串或候选列表
                    （列表按序尝试，首个提取成功即返回——兼容多版式标签）
      optional      缺省 false；true 时未提取到值不视为失败（通用模板可选字段）
      anchor_span   缺省 false；true 时锚点 x 窗口向右延伸到本列右边界
                    （相邻列表头起点）；表头被字间距切成单字（"金"+"额"）
                    时会先吸收这些碎片，使列内靠右对齐、超出表头文字范围的
                    值仍算同列，又不串到下一列
      direction     right = 同行右侧；below = 下方
      same_line     right 方向是否要求同行（默认 true）
      same_word     缺省 true；false 时不取"锚点同词尾部"（标签是长词前缀时
                    避免把标签残段当值，如锚点"货物或应税劳务"命中
                    "货物或应税劳务、服务名称"）
      max_distance  below 方向的最大垂直距离（默认 50）
      below_mode    below 取词方式：
                    prefix（默认）= 前 1/前 2/前 3 词前缀拼接候选；
                    merge = 范围内所有词按序合并为一个候选（跨行单元格完整值）；
                    each  = 范围内每个词各自成候选（配合 pattern 精确挑选）；
                    row   = 按行分组、行内拼接后各自成候选（数值被切词成
                            "118812." + "57" 这类多段时使用；同行小间距的
                            数字碎片会自动补齐，邻列内容不会并入）；
                    count = 统计同列行数（明细行数），值形如 "3"
      stop_anchor   截断关键词（任意列命中即截断）：count 模式用于排除
                    合计行/建筑服务信息块；merge 模式用于防止长文案放宽
                    max_distance 后把后续标签、合计行并进值里。
                    带字间距的标签走相邻词拼接匹配
      pattern       值的正则（fullmatch）；不匹配的候选会被跳过
      pick          first（默认）= 取首个匹配；last = 取最后一个匹配
                    （如合计行在明细下方时，取同列最下方的值）
      x_min / x_max 候选值词起始 x 的范围（pt），用于区分左右分栏
                    （如发票购方/销方栏的"名称："锚点文字完全相同）
      scope         取值分区：{start_anchor: [...], end_anchor: [...]}，把"取值"
                    限定在起始标签行之后、结束标签行之前。只过滤候选值——
                    锚点仍从全页词表查找（表头行常同时放着范围标签与锚点），
                    标签不存在时自动退回全页。用于同名字段在多处重复的场景
      region        页面区域（模板顶层 regions 定义，方案 §4/§5）：
                    header / items / totals 等。同样只过滤候选值，锚点全页查找

    候选按"与锚点的行/列重叠质量"分层：先严格同行/同列，再擦边候选兜底，
    避免"税"这类多命中锚点（纳税人识别号、税率、税额…）借用相邻列的候选。

    明细表格（模板顶层 table 配置，方案 §P0）走 Table First：
    先由 table_engine 重建整张表，再把明细字段回填（表格结果优先于逐列锚点）；
    表头找不到时明细字段保留锚点取值，结构问题记入 report.table.issues。
    """

    def parse(self, pdf_path: str, template: dict[str, Any]) -> ParseReport:
        mode = template.get("mode", "fixed")
        if mode != "dynamic":
            raise ValueError(f"DynamicRegionParser 仅支持 dynamic 模板，收到 mode={mode!r}")

        report = ParseReport(template_id=template.get("template", "unknown"), mode=mode)
        with pymupdf.open(pdf_path) as doc:
            total_pages = len(doc)
            words_cache: dict[int, list[_Word]] = {}

            def words_of(page_no: int) -> list[_Word]:
                """同一页的词表只取一次（表格与字段、多字段之间复用）。"""
                if page_no not in words_cache:
                    words_cache[page_no] = _page_words(doc[page_no])
                return words_cache[page_no]

            # ① 表格重建（Table First）：先把整张明细表重建出来。
            # 首页表格贴到页底（未命中 stop_anchor）时，向后续页拼接明细行。
            table_config = template.get("table")
            if table_config:
                table_page = int(table_config.get("page", 0))
                if table_page < total_pages:
                    report.table = extract_table_multipage(
                        (words_of(p) for p in range(table_page, total_pages)), table_config
                    )
                    report.items = list(report.table.items)

            # ② 普通字段：锚点 + 相对方向（Region → Scope → Anchor → Value），
            # 配置页取不到时跨页兜底（字段可能被排到第二页）
            for name, spec in template.get("fields", {}).items():
                report.fields[name] = _extract_field_across_pages(
                    name, spec, template, words_of, total_pages
                )

            # ③ 表格结果回填明细字段（成功时覆盖锚点取值）
            if report.table is not None:
                report.table_applied = apply_to_report(report, template, report.table)
        report.apply_business_rules(template.get("business_rules"))
        return report
