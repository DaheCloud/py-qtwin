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

from dataclasses import dataclass, field
from typing import Any

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

# "短文本即视为可信主数据"的长度上限（"1批"/"项"这类非纯数字但合法的单元格值）
_SHORT_CELL_LIMIT = 8

# 首列/末列边缘余量（pt，约 2~3 倍字高）：收口表格左右边界，
# 避免表格外的备注/序号/页边文本被无限边界吸进首列/末列
_EDGE_MARGIN_DEFAULT = 24.0

# 行比较的浮点容差（pt）
_EPS = 0.01

# 表头行拼接兜底的最大相邻词数与间距倍数（"单"+"位"这类字间距拆词）
_BAND_MERGE_SPAN = 4
_BAND_MERGE_GAP_RATIO = 2.0


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
    result.physical_rows = len(rows)

    items, trailing = _build_items(
        rows, columns, suffix_continuation=bool(config.get("suffix_continuation", True))
    )
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


# ---------------------------------------------------------------- 表头与列


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


# ---------------------------------------------------------------- 物理行 → 逻辑行


def _build_items(
    rows: list[list[Word]],
    columns: dict[str, TableColumn],
    *,
    suffix_continuation: bool = True,
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
    for row in rows:
        cells: dict[str, list[Word]] = {key: [] for key in columns}
        for word in row:
            key = _column_key_at(columns, word.cx)
            if key is not None:
                cells[key].append(word)
        if not any(cells.values()):
            continue
        if not _has_data(cells, columns):
            for key, words in cells.items():
                if words:
                    pending.setdefault(key, []).extend(words)
            continue
        merged = {key: pending.get(key, []) + cells.get(key, []) for key in columns}
        pending = {}
        items.append(_build_item(merged, columns, index=len(items) + 1))

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
    return items, pending


def _build_item(
    cells: dict[str, list[Word]], columns: dict[str, TableColumn], *, index: int
) -> dict[str, Any]:
    item: dict[str, Any] = {"row_index": index}
    for key, col in columns.items():
        item[key] = _cell_value(cells.get(key) or [], col)
    return item


def _has_data(cells: dict[str, list[Word]], columns: dict[str, TableColumn]) -> bool:
    """该物理行是否包含"可信的主数据"（数量/单价/金额/税额）。

    行级合理性守卫：只有名称/规格等跨行单元格的行不算数据行；主数据列里
    出现明显不是数值的长文本（如表格下方信息块的说明文字落进数值列）也不
    算——避免把版式噪声并成一条明细。短文本（"1批"、"项"）仍视为数据。
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
        if len(text) <= _SHORT_CELL_LIMIT:
            return True
    return False


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
        report.fields[field_name] = validate_field(result, spec)
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
