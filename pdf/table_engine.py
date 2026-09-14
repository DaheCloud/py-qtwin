"""明细表格引擎（方案 §6-§15）：Table First，而不是 Field First。

为什么需要它：
  明细字段（商品名称/规格/单位/数量/单价/金额/税率/税额）如果继续"每列单独找
  锚点、向下取值、再拼成多条明细"，会逐渐出现错行、跨列、多行单元格拼接困难、
  表头切词、多条明细关联错误。表格引擎把明细区域当作一张表重建：

    找表头 → 生成列边界 → 确定表格上下边界 → 按 Y 聚类物理行
    → 按 X 分配单元格 → 重建逻辑行（跨行单元格合并）→ 输出结构化 items

关键设计（方案 §8-§14）：
  · 表头定位：候选表头 + 字间距拆词兜底（"金"+"额" → "金额"）；
  · 列边界：相邻表头中心点取中（比"锚点向下"稳定），不依赖值的对齐方式；
  · 上下边界：表头底部 ~ stop_anchor（合 计 / 价税合计）上方；
  · 物理行 → 逻辑行：只有名称（或规格等单位）的物理行是"跨行单元格"的延续，
    向后累积直到遇到带主数据列（数量/单价/金额/税额）的行，合并为一个 item；
  · 输出 items（行关联关系在解析时已建立），而不是互相独立的列数组。

表格结果既用于业务校验（Σ明细 ≈ 合计），也回填模板里的明细字段
（item_name/quantity/amount…），并落库到 extracted_items 供 UI 展示与审计。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterator

from pdf.normalizers import normalize_amount, normalize_text
from pdf.validators import FieldResult, validate_field
from pdf.words import (
    Word,
    clean_value_text,
    group_rows,
    line_overlap,
    merged_line_words,
    merged_neighbor_words,
    row_tolerance,
)

# 表格引擎产出的字段 parser 标记（审计用：区分"锚点取值"与"表格重建取值"）
TABLE_PARSER = "pymupdf-table"

# 主数据列：这些列有值才视为"明细数据行"；否则视为名称等跨行单元格的延续。
DATA_COLUMNS = ("quantity", "unit_price", "amount", "tax")

# 明细列 → 模板字段的回填映射（Table First：表格结果优先于逐列锚点）
FIELD_BINDINGS: dict[str, str] = {
    "name": "item_name",
    "spec": "spec_model",
    "unit": "unit",
    "quantity": "quantity",
    "unit_price": "unit_price",
    "tax_rate": "tax_rate",
}

# 空单元格占位符（"—"、"－"等）
_PLACEHOLDER_CHARS = set("—–-－ \u3000")

# "短文本即视为可信主数据"的形态：数字起头 + 至多 2 个非数字字符（"1批"/"1.5件"）。
# 不能用"长度 ≤ N"粗判：重复表头的"数 量"、页脚"1/2"都会被误判为数据行。
_SHORT_NUMERIC_LIKE = re.compile(r"[\d.,]+\s*[%°]?\s*[^\d\s]{0,2}")

# 页脚噪声行（整行都匹配才算）：页码 "1/2"、"第 1 页 共 2 页"、"下载次数" 等
_PAGE_FOOTER = re.compile(
    r"^(?:第\s*\d+\s*页|共\s*\d+\s*页|\d+\s*[/／]\s*\d+|下载次数.*|[-—\s]*\d+[-—\s]*)$"
)

# 首列/末列边缘余量（pt，约 2~3 倍字高）：收口表格左右边界，
# 避免表格外的备注/序号/页边文本被无限边界吸进首列/末列
_EDGE_MARGIN_DEFAULT = 24.0

# 行比较的浮点容差（pt）
_EPS = 0.01

# 表头行拼接兜底的最大相邻词数与间距倍数（"单"+"位"这类字间距拆词）
_BAND_MERGE_SPAN = 4
_BAND_MERGE_GAP_RATIO = 2.0

# V2 列边界数据修正（方案 §8.2）：只允许小幅修正，避免数据反向污染表头结构
_BOUNDARY_MAX_SHIFT = 15.0
_BOUNDARY_SAMPLE_ROWS = 5

# 表格证据维度权重（方案 §8.3）：必需列 > 行一致性 ≒ 列对齐 > 表头覆盖率
_TABLE_EVIDENCE_WEIGHTS: dict[str, float] = {
    "required_columns": 0.35,
    "row_consistency": 0.25,
    "column_alignment": 0.25,
    "header": 0.15,
}


@dataclass
class TableColumn:
    """一列的定义与运行时状态（header 为命中的表头词，left/right 为列边界）。"""

    key: str
    headers: tuple[str, ...]
    title: str = ""
    type: str = "string"
    header: Word | None = None
    left: float = float("-inf")
    right: float = float("inf")

    @property
    def present(self) -> bool:
        return self.header is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title or self.key,
            "present": self.present,
            "headers": list(self.headers),
            "x0": round(self.header.x0, 2) if self.header else None,
            "x1": round(self.header.x1, 2) if self.header else None,
            "left": round(self.left, 2) if self.left != float("-inf") else None,
            "right": round(self.right, 2) if self.right != float("inf") else None,
        }


@dataclass
class TableResult:
    """一次表格重建的完整结果。

    items 中每行已建立列关联（方案 §15）；issues 记录结构问题
    （缺列 / 表头未找到 / 无明细行 / 行数据不完整等），供结构校验与审计使用。
    """

    columns: dict[str, TableColumn] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)
    header_y: float | None = None
    stop_y: float | None = None
    physical_rows: int = 0
    issues: list[str] = field(default_factory=list)
    # V2（方案 §8.2）：列边界来源（header / data_refined）与对齐质量分
    boundary_source: str = "header"
    alignment_score: float | None = None

    @property
    def ok(self) -> bool:
        """表头找到且至少重建出一行明细。"""
        return self.header_y is not None and bool(self.items)

    @property
    def missing_columns(self) -> list[str]:
        return [key for key, col in self.columns.items() if not col.present]

    def as_dict(self) -> dict[str, Any]:
        """审计日志用（结构化，含列边界与逐行 items）。"""
        return {
            "ok": self.ok,
            "header_y": round(self.header_y, 2) if self.header_y is not None else None,
            "stop_y": round(self.stop_y, 2) if self.stop_y is not None else None,
            "physical_rows": self.physical_rows,
            "column_count": sum(1 for c in self.columns.values() if c.present),
            "missing_columns": self.missing_columns,
            "issues": list(self.issues),
            "boundary_source": self.boundary_source,
            "alignment_score": (
                round(self.alignment_score, 4) if self.alignment_score is not None else None
            ),
            "columns": [col.as_dict() for col in self.columns.values()],
            "items": [dict(item) for item in self.items],
        }


def extract_table(words: list[Word], config: dict[str, Any], *, page: int = 0) -> TableResult:
    """按模板 table 配置重建明细表（Table First 的唯一入口，纯函数）。"""
    columns = _columns_from_config(config)
    result = TableResult(columns=columns)
    if not columns:
        result.issues.append("table_not_configured")
        return result

    header = _find_header(words, columns)
    if header is None:
        result.issues.append("table_header_not_found")
        return result
    result.header_y = header.bottom
    _assign_bounds(columns, config)
    result.issues.extend(f"table_column_missing:{key}" for key in result.missing_columns)

    stop_y = find_stop_y(words, config.get("stop_anchor"), after_y=header.bottom)
    result.stop_y = stop_y

    region = [
        w
        for w in words
        if w.y0 >= header.bottom - _EPS and (stop_y is None or w.y0 < stop_y - _EPS)
    ]
    rows = group_rows(region, tolerance=_row_tolerance(words, config))
    rows = _split_overlapping_rows(rows, columns, config)
    result.physical_rows = len(rows)

    # V2（方案 §8.2）：用前几行真实数据微调列边界（表头中心点只是初值）
    refined, alignment = _refine_bounds_from_data(columns, rows, config)
    if refined:
        result.boundary_source = "data_refined"
    result.alignment_score = alignment

    items, trailing = _build_items(
        rows, columns, suffix_continuation=bool(config.get("suffix_continuation", True))
    )
    _record_item_ambiguities(result, items)
    result.items = items
    if not items:
        result.issues.append("table_no_rows")
    else:
        for item in items:
            if not item.get("name"):
                result.issues.append(f"table_item_without_name:{item['row_index']}")
        if trailing:
            # standalone noise：suffix 归属后仍未消化的残余（数值列残余，
            # 或模板关闭 suffix_continuation 时的全部残余）→ 结构问题留痕
            result.issues.append("table_trailing_rows")
    return result


def extract_table_multipage(
    word_pages: Iterator[list[Word]], config: dict[str, Any]
) -> TableResult:
    """跨页明细表：首页正常重建；首页表格贴到页底（未命中 stop_anchor）
    时，向后续页继续拼行（明细表跨页的发票/销货清单）。

    后续页复用首页的列边界（表头通常不重复出现），行号接续首页编号；
    逐页拼接直到：某页命中 stop_anchor（合计行，表格结束）或某页拼不出
    明细行（表格已结束）。word_pages 为惰性页词表序列（首页在前）。
    """
    iterator = iter(word_pages)
    first = next(iterator, None)
    if first is None:
        result = TableResult(columns=_columns_from_config(config))
        result.issues.append("table_not_configured")
        return result

    result = extract_table(first, config)
    if result.header_y is None or result.stop_y is not None:
        return result

    header_texts = {
        col.header.text.replace(" ", "")
        for col in result.columns.values()
        if col.header is not None
    }
    suffix = bool(config.get("suffix_continuation", True))

    for words in iterator:
        stop_y = find_stop_y(words, config.get("stop_anchor"), after_y=0.0)
        region = [w for w in words if stop_y is None or w.y0 < stop_y - _EPS]
        rows = group_rows(region, tolerance=_row_tolerance(words, config))
        rows = _split_overlapping_rows(rows, result.columns, config)

        # 后续页的噪声行：重复表头行（表头底部作为区域起点，其上方的页眉
        # 一并排除）、页脚行。不做这层过滤时，表头文字会因"短文本即数据"
        # 被当成一条垃圾明细，页眉会被并进下一条明细的名称。
        kept: list[list[Word]] = []
        header_bottom: float | None = None
        for row in rows:
            if _is_page_footer_row(row):
                continue
            if _looks_like_header_row(row, header_texts, result.columns):
                bottom = max(w.y1 for w in row)
                header_bottom = bottom if header_bottom is None else min(header_bottom, bottom)
                continue
            kept.append(row)
        if header_bottom is not None:
            kept = [row for row in kept if row[0].y0 >= header_bottom - _EPS]

        new_items, trailing = _build_items(
            kept, result.columns, suffix_continuation=suffix, index_offset=len(result.items)
        )
        _record_item_ambiguities(result, new_items)
        if trailing and "table_trailing_rows" not in result.issues:
            result.issues.append("table_trailing_rows")
        if new_items:
            result.items.extend(new_items)
            result.physical_rows += len(kept)
        if stop_y is not None:
            # 合计行在本页：表格闭合。明细先并入再收尾——"明细全在第 1 页、
            # 合计行在第 2 页"时本页没有新明细，但表格确实已闭合（审计口径）
            result.stop_y = stop_y
            break
        if not new_items:
            break  # 该页没有明细行：表格已结束
    return result


@dataclass(frozen=True)
class _HeaderRow:
    """命中的表头行：bottom 为表头行底（表格内容起点），words 为各列命中的表头词。"""

    bottom: float
    words: dict[str, Word]


def _columns_from_config(config: dict[str, Any]) -> dict[str, TableColumn]:
    columns: dict[str, TableColumn] = {}
    for key, spec in (config.get("columns") or {}).items():
        if isinstance(spec, (list, tuple)):
            spec = {"headers": list(spec)}
        headers = spec.get("headers") or [spec.get("title") or key]
        columns[key] = TableColumn(
            key=key,
            headers=tuple(str(h) for h in headers),
            title=str(spec.get("title") or key),
            type=str(spec.get("type") or "string"),
        )
    return columns


def _matches(text: str, headers: tuple[str, ...]) -> bool:
    return any(header in text for header in headers)


def _find_header(words: list[Word], columns: dict[str, TableColumn]) -> _HeaderRow | None:
    """定位表头行：先按候选表头直接匹配，再做字间距拆词的拼接兜底。"""
    candidates: dict[str, list[Word]] = {}
    for key, col in columns.items():
        hits = [w for w in words if _matches(w.text, col.headers)]
        if not hits:
            hits = [w for w in merged_neighbor_words(words) if _matches(w.text, col.headers)]
        candidates[key] = hits

    all_hits = [w for hits in candidates.values() for w in hits]
    if not all_hits:
        return None

    tol = row_tolerance(words)
    best_row: list[Word] | None = None
    best_cover = 0
    for row in group_rows(all_hits, tolerance=tol):
        cover = sum(1 for hits in candidates.values() if any(w in hits for w in row))
        if cover > best_cover or (
            cover == best_cover
            and best_row is not None
            and row[0].y0 < best_row[0].y0  # 并列取最靠上的一行
        ):
            best_row, best_cover = row, cover
    if best_row is None or best_cover <= 0:
        return None

    band = _band_words(words, best_row, tol)
    for key, col in columns.items():
        in_band = [w for w in candidates[key] if _in_band(w, best_row, tol)]
        if not in_band:
            in_band = [w for w in _merged_band_words(band) if _matches(w.text, col.headers)]
        if in_band:
            # 优先整词精确命中；同分取最靠左（多列同名标签时按列序）
            col.header = min(in_band, key=lambda w: _header_rank(w.text, col.headers))
    chosen = {key: col.header for key, col in columns.items() if col.present}
    if not chosen:
        return None
    bottom = max(word.y1 for word in chosen.values())
    return _HeaderRow(bottom=bottom, words=chosen)


def _band_words(words: list[Word], row: list[Word], tol: float) -> list[Word]:
    """与表头行同一水平带（含中心重叠）的全部词，供拼接兜底使用。"""
    return [w for w in words if _in_band(w, row, tol)]


def _in_band(word: Word, row: list[Word], tol: float) -> bool:
    lo = min(w.y0 for w in row)
    hi = max(w.y1 for w in row)
    return word.y1 > lo - tol and word.y0 < hi + tol


def _merged_band_words(band: list[Word]) -> list[Word]:
    """表头带内的相邻词拼接（间距放宽到 2 倍字高，兜住字间距排版）。"""
    out: list[Word] = []
    ordered = sorted(band, key=lambda w: (w.y0, w.x0))
    for i, first in enumerate(ordered):
        span = [first]
        for nxt in ordered[i + 1 : i + _BAND_MERGE_SPAN]:
            last = span[-1]
            if not line_overlap(last, nxt) or nxt.x0 < last.x0:
                break
            if nxt.x0 - last.x1 > _BAND_MERGE_GAP_RATIO * max(last.height, 1.0):
                break
            span.append(nxt)
            out.append(
                Word(
                    span[0].x0,
                    min(w.y0 for w in span),
                    max(w.x1 for w in span),
                    max(w.y1 for w in span),
                    "".join(w.text for w in span),
                )
            )
    return out


def _header_rank(text: str, headers: tuple[str, ...]) -> tuple[int, int]:
    """表头候选排序：整词精确命中优先，其次按文本长度（越短越像表头）。"""
    exact = 0 if text.replace(" ", "") in {h.replace(" ", "") for h in headers} else 1
    return exact, len(text)


def _assign_bounds(columns: dict[str, TableColumn], config: dict[str, Any]) -> None:
    """列边界 = 相邻表头中心点的中点（方案 §9）；首列/末列用表头边缘 ± margin。

    比"锚点向下 + 固定窗口"稳定：不依赖值靠左/居中/靠右的对齐方式，
    只依赖表头本身的版式位置。

    首列/末列**不用无限边界**：真实 PDF 中表格左右可能存在备注、序号、页边
    文本，无限边界会把它们全部吸进首列/末列。实际边界收口为：
        table_left  = first_header.x0 - edge_margin
        table_right = last_header.x1  + edge_margin
    边缘外的词不归入任何列（宁缺勿错）；margin 可由模板 table.edge_margin 配置。
    """
    present = sorted(
        (col for col in columns.values() if col.present),
        key=lambda col: col.header.cx,  # type: ignore[union-attr]
    )
    if not present:
        return
    margin = max(0.0, float(config.get("edge_margin", _EDGE_MARGIN_DEFAULT)))
    first, last = present[0], present[-1]
    for i, col in enumerate(present):
        cx = col.header.cx  # type: ignore[union-attr]
        if i == 0:
            col.left = first.header.x0 - margin  # type: ignore[union-attr]
        else:
            col.left = (present[i - 1].header.cx + cx) / 2  # type: ignore[union-attr]
        if i == len(present) - 1:
            col.right = last.header.x1 + margin  # type: ignore[union-attr]
        else:
            col.right = (cx + present[i + 1].header.cx) / 2  # type: ignore[union-attr]


def _refine_bounds_from_data(
    columns: dict[str, TableColumn],
    rows: list[list[Word]],
    config: dict[str, Any],
) -> tuple[bool, float | None]:
    """用前几行真实数据微调列边界（方案 §8.2）。

    表头中心点只反映"表头文字"的位置：短表头 + 宽数据列、右对齐数字、
    金额列偏窄时边界会系统性偏移。这里取前 N 行物理行，按"相邻两列数据簇
    之间的空隙"重新取中，并且**只允许小幅修正**
    （默认 ±15pt，模板可配 ``table.boundary_max_shift``），避免数据反向污染
    表头结构。

    返回 (是否修正过, 对齐质量分 0~1)。对齐分 = 1 - 数据中心与表头中心的
    平均偏移 / 最大允许偏移，供 table evidence 使用。
    """
    present = sorted(
        (col for col in columns.values() if col.present),
        key=lambda col: col.header.cx,  # type: ignore[union-attr]
    )
    if not present or not rows:
        return False, None

    max_shift = max(0.0, float(config.get("boundary_max_shift", _BOUNDARY_MAX_SHIFT)))
    words = [w for row in rows[:_BOUNDARY_SAMPLE_ROWS] for w in row]
    if not words:
        return False, None

    centers: dict[str, list[float]] = {col.key: [] for col in present}
    for word in words:
        key = _column_key_at(columns, word.cx)
        if key in centers:
            centers[key].append(word.cx)

    shifts: list[float] = []
    for col in present:
        values = centers.get(col.key) or []
        if not values:
            continue
        values.sort()
        data_center = values[len(values) // 2]
        shifts.append(abs(data_center - col.header.cx))  # type: ignore[union-attr]
    alignment = None
    if shifts:
        avg_shift = sum(shifts) / len(shifts)
        alignment = max(0.0, min(1.0, 1.0 - avg_shift / max(max_shift, 1.0)))

    new_bounds: dict[str, tuple[float, float]] = {}
    prev_right: float | None = None
    for i, col in enumerate(present):
        left = col.left if i == 0 else _shift_boundary(col.left, words, max_shift)
        if i == len(present) - 1:
            right = col.right
        else:
            right = _shift_boundary(col.right, words, max_shift)
        if prev_right is not None:
            left = max(left, prev_right)  # 保证边界单调，列不重叠
        if right <= left:
            left, right = col.left, col.right  # 修正失败：退回表头边界
        new_bounds[col.key] = (left, right)
        prev_right = right

    refined = any(
        abs(new_bounds[col.key][0] - col.left) > _EPS
        or abs(new_bounds[col.key][1] - col.right) > _EPS
        for col in present
    )
    for col in present:
        col.left, col.right = new_bounds[col.key]
    return refined, alignment


def _shift_boundary(boundary: float, words: list[Word], max_shift: float) -> float:
    """把一条内部边界移到"左右两列数据簇空隙"的中间；偏移超限则不动。"""
    near_left = [w.cx for w in words if boundary - max_shift <= w.cx < boundary]
    near_right = [w.cx for w in words if boundary <= w.cx <= boundary + max_shift]
    if not near_left or not near_right:
        return boundary
    candidate = (max(near_left) + min(near_right)) / 2
    if abs(candidate - boundary) > max_shift:
        return boundary
    return candidate


def table_evidence(result: TableResult, config: dict[str, Any]) -> dict[str, float]:
    """表格证据（方案 §8.3）：表头命中 / 必需列 / 行一致性 / 列对齐。"""
    columns = result.columns
    total = len(columns)
    present = sum(1 for col in columns.values() if col.present)
    header_score = present / total if total else 0.0

    required = list(config.get("required_columns") or [])
    if required:
        missing = [
            key
            for key in required
            if not (columns.get(key) is not None and columns[key].present)
        ]
        required_score = 1.0 - len(missing) / len(required)
    else:
        required_score = header_score

    if result.items:
        solid = sum(
            1
            for item in result.items
            if item.get("name") and any(item.get(key) for key in DATA_COLUMNS)
        )
        row_score = solid / len(result.items)
    else:
        row_score = 0.0

    scores = {
        "header": header_score,
        "required_columns": required_score,
        "row_consistency": row_score,
    }
    if result.alignment_score is not None:
        scores["column_alignment"] = result.alignment_score
    return scores


def table_score(result: TableResult | None, config: dict[str, Any] | None) -> float | None:
    """表格综合分 0~1；未配置表格或无列可评时返回 None（不参与文档分）。

    加权而非等权：**模板声明的必需列**（required_columns）是否齐全最能说明
    表格是否可用；"所有配置列都出现"不可靠——不同版式本就没有全部列
    （建筑服务数电票没有"建筑服务发生地"表头，它在信息块里），等权会把
    正常表格拖到低分。
    """
    if result is None or not config:
        return None
    scores = table_evidence(result, config)
    parts = [
        (weight, scores[name])
        for name, weight in _TABLE_EVIDENCE_WEIGHTS.items()
        if name in scores
    ]
    total = sum(weight for weight, _ in parts)
    if total <= 0:
        return None
    return sum(weight * value for weight, value in parts) / total


def _column_key_at(columns: dict[str, TableColumn], cx: float) -> str | None:
    for key, col in columns.items():
        if col.present and col.left <= cx < col.right:
            return key
    return None


# ---------------------------------------------------------------- 表格边界


def find_stop_y(words: list[Word], keywords: list[str] | None, *, after_y: float) -> float | None:
    """stop_anchor 命中处的最小 y（表格下边界）；未配置或无命中返回 None。

    任意列命中即算；带字间距的标签（"合 计" → "合"+"计"）通过相邻词/整行
    拼接兜底匹配。**三级命中的结果必须合并取 min**（与锚点引擎
    _stop_boundary_y 同语义）：直匹配可能只命中更下方的标签（如"价税合计
    （大写）"），而上方真正需要截断的"合 计"行只有整行拼接才能命中——
    逐级短路会漏掉它，把合计/价税合计行混进明细。
    """
    if not keywords:
        return None
    below = [w for w in words if w.y0 >= after_y - _EPS]
    boundaries = [w.y0 for w in below if _contains(w.text, keywords)]
    boundaries.extend(
        w.y0 for w in merged_neighbor_words(below) if _contains(w.text, keywords)
    )
    boundaries.extend(
        w.y0 for w in merged_line_words(below) if _contains(w.text, keywords)
    )
    return min(boundaries) if boundaries else None


def _contains(text: str, keywords: list[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _row_tolerance(words: list[Word], config: dict[str, Any]) -> float:
    """行聚类容差：配置值与字高比例取大（配置只用于放宽，不用于收紧）。"""
    base = row_tolerance(words)
    configured = config.get("row_tolerance")
    if configured is None:
        return base
    return max(base, float(configured))


def _split_overlapping_rows(
    rows: list[list[Word]],
    columns: dict[str, TableColumn],
    config: dict[str, Any],
) -> list[list[Word]]:
    """将被宽松初始容差误合并的两条数据行按 y 层重新拆开。

    只在至少两个非文本列均出现多个 y 层的完整标量值时触发。单行金额被切成
    ``73933.`` + ``20``、或仅一个单元格重复，不足以触发拆行，避免过度修正。
    """
    refined: list[list[Word]] = []
    configured = config.get("row_split_tolerance")
    for row in rows:
        heights = sorted(w.height for w in row if w.height > 0)
        median_height = heights[len(heights) // 2] if heights else 1.0
        split_tolerance = (
            max(0.5, float(configured))
            if configured is not None
            else max(0.75, median_height * 0.15)
        )

        scalar_words: dict[str, list[Word]] = {}
        for word in row:
            key = _column_key_at(columns, word.cx)
            if key is not None and columns[key].type != "string":
                scalar_words.setdefault(key, []).append(word)

        repeated_columns = 0
        for key, words in scalar_words.items():
            layers = group_rows(words, tolerance=split_tolerance)
            complete_layers = sum(
                _complete_scalar_value(layer, columns[key].type) for layer in layers
            )
            if complete_layers >= 2:
                repeated_columns += 1

        split = group_rows(row, tolerance=split_tolerance)
        if repeated_columns >= 2 and len(split) >= 2:
            refined.extend(split)
        else:
            refined.append(row)
    return refined


def _complete_scalar_value(words: list[Word], column_type: str) -> bool:
    """一个 y 层是否构成完整数值；小数碎片末尾 ``.`` 不算完整。"""
    text = clean_value_text("".join(w.text for w in sorted(words, key=lambda w: w.x0))).strip()
    if column_type == "rate":
        return bool(re.fullmatch(r"\d+(?:\.\d+)?%", text))
    return bool(re.fullmatch(r"[¥￥]?[+-]?\d[\d,]*(?:\.\d+)?", text))


# ---------------------------------------------------------------- 物理行 → 逻辑行


def _build_items(
    rows: list[list[Word]],
    columns: dict[str, TableColumn],
    *,
    suffix_continuation: bool = True,
    index_offset: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, list[Word]]]:
    """物理行 → 逻辑行 → items（方案 §12-§14）。

    非数据行的三类归属（prefix / suffix / standalone noise）：

    · prefix continuation —— 名称行**之后跟着数据行**：并入下一条明细
      （单元格折行的常态：名称先折行、数值与最后一行名称同行）；
    · suffix continuation —— 表尾名称行**后面没有数据行**：并入上一条明细
      （"商品A 2 100 200 9% 18" 下方的附加说明文字属于上一行，而不是下一条
      商品）。只并入 string 列（名称/规格/单位），数值列残余不并入，
      避免污染数量/金额；模板可用 table.suffix_continuation=false 关闭；
    · standalone noise —— suffix 归属后仍未消化的残余（数值列残余，或关闭
      suffix 时的全部残余）：单独返回，由调用方记 table_trailing_rows。
    """
    items: list[dict[str, Any]] = []
    pending: dict[str, list[Word]] = {}
    noise: dict[str, list[Word]] = {}
    for row in rows:
        cells: dict[str, list[Word]] = {key: [] for key in columns}
        for word in row:
            key = _column_key_at(columns, word.cx)
            if key is not None:
                cells[key].append(word)
        if not any(cells.values()):
            continue
        if _is_page_footer_row(row):
            continue  # 页脚噪声（页码等）：单页贴页底时同样要丢弃
        if not _has_data(cells, columns):
            for key, words in cells.items():
                if words:
                    # 跨行延续只适用于名称/规格/单位等文本。税率、金额等数值
                    # 带入下一行会形成 20%20% / 100.00100.00 这类静默拼接。
                    target = pending if columns[key].type == "string" else noise
                    target.setdefault(key, []).extend(words)
            continue
        merged = {key: pending.get(key, []) + cells.get(key, []) for key in columns}
        pending = {}
        items.append(_build_item(merged, columns, index=len(items) + 1 + index_offset))

    if pending:
        string_keys = [key for key, col in columns.items() if col.type == "string"]
        if items and suffix_continuation and any(pending.get(key) for key in string_keys):
            last = items[-1]
            for key in string_keys:
                words = pending.get(key)
                if not words:
                    continue
                text = _cell_value(words, columns[key])
                if text:
                    last[key] = f"{last[key]} {text}" if last.get(key) else text
            # 文本残余已归属上一条；剩余的数值列残余按 noise 返回
            pending = {
                key: words
                for key, words in pending.items()
                if words and columns[key].type != "string"
            }
    trailing = {key: list(words) for key, words in noise.items()}
    for key, words in pending.items():
        trailing.setdefault(key, []).extend(words)
    return items, trailing


def _build_item(
    cells: dict[str, list[Word]], columns: dict[str, TableColumn], *, index: int
) -> dict[str, Any]:
    item: dict[str, Any] = {"row_index": index}
    ambiguous: list[str] = []
    for key, col in columns.items():
        words = cells.get(key) or []
        if col.type == "rate" and _rate_is_ambiguous(words):
            item[key] = None
            ambiguous.append(key)
        else:
            item[key] = _cell_value(words, col)
    if ambiguous:
        item["_ambiguous_columns"] = ambiguous
    return item


def _rate_is_ambiguous(words: list[Word]) -> bool:
    """同一物理单元格出现两个完整税率时拒绝拼接，数字与 % 分词仍允许。"""
    raw = "".join(w.text.strip() for w in sorted(words, key=lambda w: (w.y0, w.x0)))
    return len(re.findall(r"\d+(?:\.\d+)?%", raw)) > 1


def _record_item_ambiguities(result: TableResult, items: list[dict[str, Any]]) -> None:
    """把内部歧义标记提升为表格问题，避免隐藏键进入持久化与审计 items。"""
    for item in items:
        for key in item.pop("_ambiguous_columns", []):
            result.issues.append(f"table_cell_ambiguous:{item.get('row_index')}:{key}")


def _has_data(cells: dict[str, list[Word]], columns: dict[str, TableColumn]) -> bool:
    """该物理行是否包含"可信的主数据"（数量/单价/金额/税额）。

    行级合理性守卫：只有名称/规格等跨行单元格的行不算数据行；主数据列里
    出现明显不是数值的文本也不算——包括表格下方信息块的长说明文字、
    重复表头的"数 量"、页脚的"1/2"。只有"数字起头 + 至多 2 个非数字字符"
    （"1批"/"1.5件"）这类合法的非纯数字单元格才仍视为数据。
    """
    for key in DATA_COLUMNS:
        if key not in columns:
            continue
        words = cells.get(key)
        if not words:
            continue
        text = clean_value_text("".join(w.text for w in words)).strip()
        if not text or all(ch in _PLACEHOLDER_CHARS for ch in text):
            continue
        if normalize_amount(text) is not None:
            return True
        if _SHORT_NUMERIC_LIKE.fullmatch(text):
            return True
    return False


def _is_page_footer_row(row: list[Word]) -> bool:
    """整行都是页脚噪声（页码/"第 N 页 共 M 页"/"下载次数" 等）→ 丢弃。"""
    texts = [w.text.strip() for w in row if w.text.strip()]
    return bool(texts) and all(_PAGE_FOOTER.fullmatch(text) for text in texts)


def _looks_like_header_row(
    row: list[Word], header_texts: set[str], columns: dict[str, TableColumn]
) -> bool:
    """该物理行是否像"重复表头行"（跨页续表的后续页常重复表头）。

    双判据（都必须"行内没有可解析数值"，避免误伤真实明细）：
      · 文本判据：行内多数词的文本（去空格）命中首页表头词；
      · 位置判据：行内多数词落在表头列中心附近，且覆盖 ≥2 个不同列
        （覆盖拆词表头：第 2 页"数"+"量"两个词与首页"数 量"文本不同，
        但位置仍在数量列）。单列折行行（如只有名称的行）不会被位置判据误伤。
    """
    if not row:
        return False
    for key in DATA_COLUMNS:
        col = columns.get(key)
        if col is None or not col.present:
            continue
        for word in row:
            if _column_key_at(columns, word.cx) == key and normalize_amount(word.text) is not None:
                return False  # 行内含可解析数值：真实数据行
    text_hits = sum(1 for w in row if w.text.replace(" ", "") in header_texts)
    if len(row) >= 2 and text_hits / len(row) >= 0.6:
        return True
    near_columns = {
        _column_key_at(columns, w.cx)
        for w in row
        if _near_header_center(w, columns)
    }
    near_columns.discard(None)
    near = len([w for w in row if _near_header_center(w, columns)])
    return len(row) >= 2 and len(near_columns) >= 2 and near / len(row) >= 0.6


def _near_header_center(word: Word, columns: dict[str, TableColumn]) -> bool:
    """词中心是否贴近某列的表头中心（列宽的 40% 以内）。"""
    key = _column_key_at(columns, word.cx)
    if key is None:
        return False
    header = columns[key].header
    if header is None:
        return False
    width = max(header.width, 1.0)
    return abs(word.cx - header.cx) <= 0.4 * width


def _cell_value(words: list[Word], col: TableColumn) -> str | None:
    """单元格取值：按 x 排序拼接（数值无分隔拼接）→ 清洗 → 按列类型标准化。"""
    if not words:
        return None
    ordered = sorted(words, key=lambda w: w.x0)
    raw = (
        " ".join(w.text for w in ordered)
        if col.type == "string"
        else "".join(w.text for w in ordered)
    )
    text = clean_value_text(raw).strip()
    if not text or all(ch in _PLACEHOLDER_CHARS for ch in text):
        return None
    if col.type == "decimal":
        amount = normalize_amount(text)
        return format(amount, "f") if amount is not None else text
    return normalize_text(text)


# ---------------------------------------------------------------- 回填字段


def apply_to_report(report: Any, template: dict[str, Any], table: TableResult) -> list[str]:
    """把表格重建结果回填到模板的明细字段，返回被回填的字段名列表。

    规则（方案 §P0：明细字段切换到 Table Engine）：
      · 表格重建成功（有 items）时，明细首行字段以表格结果为准（覆盖锚点取值）；
      · 行数类合成字段（item_rows / item_amount_rows / item_tax_rows）由 items 统计，
        不再依赖锚点 count 模式；
      · 表格未重建成功时不改动任何字段（调用方回退锚点结果）。
    """
    fields = template.get("fields", {})
    if not table.items or not fields:
        return []
    applied: list[str] = []
    first = table.items[0]
    for col_key, field_name in FIELD_BINDINGS.items():
        spec = fields.get(field_name)
        if spec is None or col_key not in table.columns:
            continue
        value = first.get(col_key)
        if not value:
            continue
        result = FieldResult(
            field_name=field_name, raw_value=value, normalized_value=value, parser=TABLE_PARSER
        )
        validated = validate_field(result, spec)
        validated.strategy = "table"
        # V2（方案 §11）：表格值覆盖锚点值时，保留锚点结果作为候选参与选优
        previous = report.fields.get(field_name)
        if previous is not None:
            report.table_replaced[field_name] = previous
        report.fields[field_name] = validated
        applied.append(field_name)

    synthetic: dict[str, str] = {"item_rows": str(len(table.items))}
    if "amount" in table.columns:
        synthetic["item_amount_rows"] = str(sum(1 for it in table.items if it.get("amount")))
    if "tax" in table.columns:
        synthetic["item_tax_rows"] = str(sum(1 for it in table.items if it.get("tax")))
    for field_name, value in synthetic.items():
        if field_name not in fields:
            continue
        report.fields[field_name] = FieldResult(
            field_name=field_name, raw_value=value, normalized_value=value, parser=TABLE_PARSER
        )
        applied.append(field_name)
    return applied
