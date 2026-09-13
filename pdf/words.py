"""统一词元模型与版面工具（Region / Table / Anchor 三类引擎共用同一份 Words 口径）。

为什么单独成模块（方案 §30）：
  文本层、pdfplumber、未来 OCR 的输出统一成同一种 {text, x0, y0, x1, y1} 结构，
  Region / Anchor / Table / Validation 直接复用，不因取词来源不同而分叉。

这些实现来自 dynamic_parser 的原私有函数（行为保持不变），dynamic_parser 以
`_xxx = xxx` 别名导入；既有调用方与测试（`from pdf.dynamic_parser import _Word`）
不受影响。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pymupdf

# 锚点与值之间的分隔符（同词情形）：全角/半角冒号、空格
SEPARATORS = "：: \u3000"

# 值尾部的空单元格占位符：发票表格常用"—"占位。相邻单元格内容与值间隔很小时
# PyMuPDF 切词会把占位符并入同一个 word（跨列粘连，如 "*服务*名称—"），
# 取值时统一清除尾部横线（值以横线结尾无实际语义）。
TRAILING_PLACEHOLDERS = "—–-－"

# 相邻词拼接的间距上限（相对词高比例）：实票表头常按字间距排版，切词后
# "单 位"变成"单"+"位"两个词，整词锚点需拼接后才能命中。
# 实测数电票（发票4）表头"税 额"切成"税"+"额"，字距 13.5pt ≈ 1.45 倍字高，
# 1.2 倍阈值拼不上 → "税 额/税额"锚点全部落空，级联到宽泛锚点"税"会命中
# "税率/征收率"列头取错列。取 1.6 倍覆盖 1~1.5 倍字距的表头排版。
ANCHOR_MERGE_GAP_RATIO = 1.6

# 纯数字碎片（金额被切词成 "118812." + "57" 这类多段）
NUMBER_FRAGMENT = re.compile(r"^[\d,．.\s]+$")

# 行聚类容差：字高比例与固定下限取大（pt）
ROW_TOLERANCE_MIN = 2.0
ROW_TOLERANCE_RATIO = 0.5


@dataclass(frozen=True)
class Word:
    """一个词元（与 PyMuPDF get_text("words") / pdfplumber extract_words 同构）。

    frozen=True：可哈希、可比较，便于去重与集合运算；构造参数顺序与既有
    dynamic_parser._Word 完全一致（x0, y0, x1, y1, text）。
    """

    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def cx(self) -> float:
        """水平中心（列归属判定用）。"""
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        """垂直中心（行归属判定用）。"""
        return (self.y0 + self.y1) / 2

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    def as_dict(self) -> dict[str, float | str]:
        """审计/序列化用。"""
        return {"text": self.text, "x0": self.x0, "y0": self.y0, "x1": self.x1, "y1": self.y1}


def page_words(page: pymupdf.Page) -> list[Word]:
    """PyMuPDF 页面 → 统一 Word 列表。"""
    return [Word(w[0], w[1], w[2], w[3], w[4]) for w in page.get_text("words")]


def strip_leading_separators(text: str) -> str:
    return text.lstrip(SEPARATORS)


def clean_value_text(text: str) -> str:
    """候选值清洗：去前导分隔符 + 去尾部占位横线（跨列粘连的"—"）。"""
    return strip_leading_separators(text).rstrip(TRAILING_PLACEHOLDERS)


def line_overlap(a: Word, b: Word) -> bool:
    """两词在垂直方向上重叠，视为同一行。"""
    return b.y0 < a.y1 and b.y1 > a.y0


def x_overlap(a: Word, b: Word) -> bool:
    return b.x0 < a.x1 and b.x1 > a.x0


def row_tolerance(words: list[Word]) -> float:
    """行聚类容差：字高的一半（下限 2pt）。"""
    if not words:
        return ROW_TOLERANCE_MIN
    height = max(w.height for w in words) or 1.0
    return max(ROW_TOLERANCE_MIN, ROW_TOLERANCE_RATIO * height)


def group_rows(words: list[Word], tolerance: float | None = None) -> list[list[Word]]:
    """按 y 坐标把词聚成物理行，行内按 x 排序（返回行列表）。

    与旧实现（_join_by_row / _count_rows 内部）一致：以每行首个词的 y0 为基准，
    后续词 y0 与基准差 ≤ 容差即归入同一行。
    """
    if not words:
        return []
    tol = tolerance if tolerance is not None else row_tolerance(words)
    ordered = sorted(words, key=lambda w: (w.y0, w.x0))
    rows: list[list[Word]] = [[ordered[0]]]
    for word in ordered[1:]:
        if word.y0 - rows[-1][0].y0 <= tol:
            rows[-1].append(word)
        else:
            rows.append([word])
    for row in rows:
        row.sort(key=lambda w: w.x0)
    return rows


def count_rows(words: list[Word]) -> int:
    """同列词按 y 聚成行后的行数（同一行内的多个词只算一行）。"""
    return len(group_rows(words))


def join_by_row(words: list[Word]) -> list[Word]:
    """把同列词按 y 分组成行，行内按 x 顺序拼接为一个候选值。

    实票里数值常被切词成多段（"118812." + "57"，或货币符号单独成词），
    按行拼接才能还原成完整数值。
    """
    return [
        Word(
            row[0].x0,
            min(w.y0 for w in row),
            max(w.x1 for w in row),
            max(w.y1 for w in row),
            "".join(w.text for w in row),
        )
        for row in group_rows(words)
    ]


def merged_neighbor_words(words: list[Word], max_span: int = 3) -> list[Word]:
    """把同行内间距很小的相邻词拼接成合并词（供整词匹配的兜底）。

    同时产出"无空格"（单位）与"带空格"（单 位）两种写法，兼容实票排版。
    """
    out: list[Word] = []
    for i, first in enumerate(words):
        span = [first]
        for nxt in words[i + 1 : i + max_span]:
            last = span[-1]
            if not line_overlap(last, nxt) or nxt.x0 < last.x0:
                break
            gap_tol = max(4.0, ANCHOR_MERGE_GAP_RATIO * max(last.height, 1.0))
            if nxt.x0 - last.x1 > gap_tol:
                break
            span.append(nxt)
            x0, x1 = span[0].x0, max(w.x1 for w in span)
            y0, y1 = min(w.y0 for w in span), max(w.y1 for w in span)
            out.append(Word(x0, y0, x1, y1, "".join(w.text for w in span)))
            out.append(Word(x0, y0, x1, y1, " ".join(w.text for w in span)))
    return out


def merged_line_words(words: list[Word]) -> list[Word]:
    """把同一行的词按 x 顺序拼成"整行词"（含带空格写法），供关键词兜底匹配。

    只用于只关心 y 的场景（截断线、范围/区域边界判断）：整行词的 bbox 横跨
    多列，不能用于取值。
    """
    lines: list[list[Word]] = []
    for word in sorted(words, key=lambda w: (w.y0, w.x0)):
        for line in lines:
            if line_overlap(line[0], word):
                line.append(word)
                break
        else:
            lines.append([word])

    out: list[Word] = []
    for line in lines:
        line.sort(key=lambda w: w.x0)
        text = "".join(w.text for w in line)
        spaced = " ".join(w.text for w in line)
        x0, x1 = line[0].x0, max(w.x1 for w in line)
        y0, y1 = min(w.y0 for w in line), max(w.y1 for w in line)
        out.append(Word(x0, y0, x1, y1, text))
        out.append(Word(x0, y0, x1, y1, spaced))
    return out


def with_number_fragments(below: list[Word], words: list[Word]) -> list[Word]:
    """把同行的"数值碎片"并入候选列表。

    实票数值常被切词成多段（"118812." + "57"），右侧碎片往往不落在候选的 x
    范围内；这里按"同行 + 小间距 + 内容像数字"补齐，供按行拼接还原。
    只吸收纯数字/逗号/小数点的碎片，避免把邻列的"3%"之类并进来。
    """
    extra: list[Word] = []
    for word in below:
        gap_tol = max(4.0, ANCHOR_MERGE_GAP_RATIO * max(word.height, 1.0))
        for other in words:
            if other is word or not line_overlap(word, other) or other.x0 < word.x1:
                continue
            if other.x0 - word.x1 <= gap_tol and NUMBER_FRAGMENT.fullmatch(other.text):
                extra.append(other)
    merged = list(below)
    for word in extra:
        if word not in merged:
            merged.append(word)
    return sorted(merged, key=lambda w: (w.y0, w.x0))
