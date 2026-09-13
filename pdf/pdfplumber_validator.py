"""pdfplumber 交叉验证：只针对关键字段二次提取并比较标准化结果。

固定字段（rect）：pdfplumber 在同一矩形区域内重新取词拼接比对；
动态字段（anchor）：pdfplumber 用自己的切词引擎独立取词，
跑与 PyMuPDF 完全相同的锚点规则（extract_anchor_field）后比对。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pdfplumber

from pdf.dynamic_parser import _Word, extract_field_cross_page
from pdf.normalizers import normalize_amount, normalize_date, normalize_text
from pdf.pymupdf_parser import ParseReport


@dataclass
class VerificationOutcome:
    """单个字段的交叉验证结果。"""

    field_name: str
    primary_value: str | None
    secondary_value: str | None
    matched: bool


@dataclass
class VerificationReport:
    outcomes: list[VerificationOutcome] = field(default_factory=list)

    @property
    def mismatches(self) -> list[VerificationOutcome]:
        return [o for o in self.outcomes if not o.matched]

    @property
    def all_matched(self) -> bool:
        return not self.mismatches


class PdfplumberValidator:
    """关键字段用 pdfplumber 在同一 rect 内重新提取并比较。"""

    _NORMALIZERS: dict[str, Any] = {
        "string": normalize_text,
        "decimal": normalize_amount,
        "date": normalize_date,
    }

    def verify(self, pdf_path: str, template: dict[str, Any], primary: ParseReport) -> VerificationReport:
        report = VerificationReport()
        verify_fields = {
            name: spec
            for name, spec in template.get("fields", {}).items()
            if spec.get("verify")
        }
        if not verify_fields:
            return report

        with pdfplumber.open(pdf_path) as pdf:
            # 页词表懒加载缓存：跨页兜底时同一页只切词一次
            words_cache: dict[int, tuple[list[_Word], list[_Word]]] = {}

            def page_words_loose_strict(page_no: int) -> tuple[list[_Word], list[_Word]]:
                if page_no not in words_cache:
                    page = pdf.pages[page_no]
                    # 标签与值字距差异大：宽松切词（x_tolerance=8）合成完整标签词，
                    # 保守切词（默认 3）保证值不跨列粘连——宽松找锚点、保守取值。
                    words_cache[page_no] = (
                        [
                            _Word(w["x0"], w["top"], w["x1"], w["bottom"], w["text"])
                            for w in page.extract_words(x_tolerance=8)
                        ],
                        [
                            _Word(w["x0"], w["top"], w["x1"], w["bottom"], w["text"])
                            for w in page.extract_words()
                        ],
                    )
                return words_cache[page_no]

            for name, spec in verify_fields.items():
                primary_result = primary.fields.get(name)
                if primary_result is None or not primary_result.valid:
                    continue

                if "rect" in spec:
                    page_no = int(spec.get("page", 0))
                    if page_no >= len(pdf.pages):
                        continue
                    page = pdf.pages[page_no]
                    # 固定字段：在区域内按词收集文本（保留空格），作为 pdfplumber 的提取结果
                    x0, top, x1, bottom = spec["rect"]
                    words = page.within_bbox((x0, top, x1, bottom)).extract_words()
                    raw = " ".join(w["text"] for w in words).strip()
                    normalizer = self._NORMALIZERS.get(spec.get("type", "string"), normalize_text)
                    secondary = normalizer(raw) if raw else None
                    if isinstance(secondary, Decimal):
                        secondary = format(secondary, "f")
                else:
                    # 动态字段：pdfplumber 独立切词后跑与 PyMuPDF 完全相同的
                    # 跨页提取策略（配置页优先 → 页序兜底 → 跨页画布兜底），
                    # 保证双引擎的取值口径一致。
                    def _strict_of(p: int) -> list[_Word]:
                        return page_words_loose_strict(p)[1]

                    def _loose_of(p: int) -> list[_Word]:
                        return page_words_loose_strict(p)[0]

                    secondary_result = extract_field_cross_page(
                        name,
                        spec,
                        template,
                        page_words_fn=_strict_of,
                        total_pages=len(pdf.pages),
                        anchor_words_fn=_loose_of,
                        parser_name="pdfplumber-dynamic",
                    )
                    secondary = secondary_result.normalized_value

                matched = (
                    secondary is not None
                    and primary_result.normalized_value is not None
                    and secondary == primary_result.normalized_value
                )
                report.outcomes.append(
                    VerificationOutcome(
                        field_name=name,
                        primary_value=primary_result.normalized_value,
                        secondary_value=secondary,
                        matched=matched,
                    )
                )
        return report
