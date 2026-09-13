"""整页文本层面的辅助校验（与锚点解析互补，见 §A/B/C 三层）。

  1. 无文本层检测：扫描件直接给出"需 OCR"的结论，不再逐字段报"未找到锚点"；
  2. 取值存在性校验：提取出的原始值必须能在原文文本中找到——字段级交叉验证
     两边共用同一套锚点规则，只能证明"切词一致"，证明不了"位置取对了"；
     整页文本是独立信息源，能抓两类问题：
       · 两个引擎一致地取错（锚点指错地方）
       · 拼接产物其实不在原文里（跨行/跨列拼出来的值）
  3. 文本一致性指标（PyMuPDF vs pdfplumber 相似度）：**仅记录，不作判定**——
     阅读顺序差异会让相似度掉到 90% 上下（字段其实全对），当门禁误报率高。

取值存在性只做"提示"（advisory）：写进原因与审计日志，不单独改变文档状态，
避免折行/顺序差异这类合法情况把整单判挂。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

import pymupdf

# 相似度只在文本不长时计算（避免长文档做 O(n^2) 比对拖慢解析）
_SIMILARITY_CHAR_LIMIT = 20000

# 合成值字段的 below_mode：这类字段的值是算出来的（明细行数），本就不在原文中
_SYNTHETIC_BELOW_MODES = {"count"}


@dataclass
class TextAudit:
    """一次整页文本校验的结果。"""

    has_text: bool
    char_count: int
    similarity: float | None = None  # C：PyMuPDF vs pdfplumber（仅记录）
    missing_values: list[tuple[str, str]] = field(default_factory=list)  # B：取值存在性


def pages_of(template: dict[str, Any] | None) -> list[int]:
    """模板字段涉及的页码（去重升序）；无模板时只看第 1 页。"""
    if not template:
        return [0]
    pages = {int(spec.get("page", 0)) for spec in template.get("fields", {}).values()}
    return sorted(pages) or [0]


def strip_ws(text: str | None) -> str:
    """去掉全部空白（含全角空格）：文本层常带字间距空格，比对前统一去除。"""
    return "".join(text.split()) if text else ""


def page_text(pdf_path: str, pages: list[int]) -> str:
    """PyMuPDF 整页文本（越界页按空串处理）。"""
    out: list[str] = []
    with pymupdf.open(pdf_path) as doc:
        for page_no in pages:
            if 0 <= page_no < len(doc):
                out.append(doc[page_no].get_text("text"))
    return "\n".join(out)


def pdfplumber_text(pdf_path: str, pages: list[int]) -> str:
    """pdfplumber 整页文本：取值存在性的第二判定（少一个引擎漏切就少一次误报）。"""
    import pdfplumber

    out: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_no in pages:
            if 0 <= page_no < len(pdf.pages):
                out.append(pdf.pages[page_no].extract_text() or "")
    return "\n".join(out)


def similarity(a: str, b: str) -> float | None:
    """两段文本的相似度（0~1）；任一为空或过长时返回 None（仅记录用）。"""
    x, y = strip_ws(a), strip_ws(b)
    if not x or not y or max(len(x), len(y)) > _SIMILARITY_CHAR_LIMIT:
        return None
    return SequenceMatcher(None, x, y).ratio()


def missing_values(
    template: dict[str, Any], report: Any, texts: list[str]
) -> list[tuple[str, str]]:
    """找出"原始值未出现在任一文本中"的字段（纯函数，便于测试）。

    - 只检查有原始值的字段；below_mode=count 这类合成值（明细行数）跳过；
    - 用 raw_value 而非 normalized_value：日期 2026-09-09 在原文里是
      2026年09月09日，金额原文可能带 ¥ 或字间距空格；
    - 任一文本命中即算找到（宽松优先，减少折行/阅读顺序差异的误报）。
    """
    haystacks = [strip_ws(text) for text in texts if text]
    missing: list[tuple[str, str]] = []
    if not haystacks:
        return missing
    for name, spec in template.get("fields", {}).items():
        if spec.get("below_mode") in _SYNTHETIC_BELOW_MODES:
            continue
        result = report.fields.get(name)
        raw = strip_ws(getattr(result, "raw_value", None))
        if not result or not raw:
            continue
        if not any(raw in haystack for haystack in haystacks):
            missing.append((name, raw))
    return missing


def audit_document(pdf_path: str, template: dict[str, Any] | None, report: Any) -> TextAudit:
    """执行 A + B + C：无文本层 / 取值存在性 / 文本一致性（C 仅记录）。"""
    pages = pages_of(template)
    fitz_text = page_text(pdf_path, pages)
    audit = TextAudit(has_text=bool(strip_ws(fitz_text)), char_count=len(strip_ws(fitz_text)))
    if not audit.has_text or template is None or report is None:
        return audit

    texts = [fitz_text]
    if audit.char_count <= _SIMILARITY_CHAR_LIMIT:
        # 只有短文档才再做一次 pdfplumber 抽取：既用于相似度记录，也作为
        # 取值存在性的第二判定
        plumber = pdfplumber_text(pdf_path, pages)
        audit.similarity = similarity(fitz_text, plumber)
        texts.append(plumber)
    audit.missing_values = missing_values(template, report, texts)
    return audit
