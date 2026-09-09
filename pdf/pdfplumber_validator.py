"""pdfplumber 交叉验证：只针对关键字段二次提取并比较标准化结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pdfplumber

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
            for name, spec in verify_fields.items():
                primary_result = primary.fields.get(name)
                if primary_result is None or not primary_result.valid:
                    continue

                page_no = int(spec.get("page", 0))
                if page_no >= len(pdf.pages):
                    continue
                page = pdf.pages[page_no]
                x0, top, x1, bottom = spec["rect"]

                # 在区域内按词收集文本（保留空格），作为 pdfplumber 的提取结果
                words = page.within_bbox((x0, top, x1, bottom)).extract_words()
                raw = " ".join(w["text"] for w in words).strip()

                normalizer = self._NORMALIZERS.get(spec.get("type", "string"), normalize_text)
                secondary = normalizer(raw) if raw else None
                if isinstance(secondary, Decimal):
                    secondary = format(secondary, "f")
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
