"""固定区域解析器：PyMuPDF 按 page + rect 提取字段（主解析路径）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pymupdf

from pdf.normalizers import normalize_amount, normalize_date, normalize_text
from pdf.validators import FieldResult, validate_field


@dataclass
class ParseReport:
    """一次模板解析的完整报告。"""

    template_id: str
    mode: str
    fields: dict[str, FieldResult] = field(default_factory=dict)
    business_errors: list[str] = field(default_factory=list)
    used_fallback: bool = False

    @property
    def valid(self) -> bool:
        return all(f.valid for f in self.fields.values()) and not self.business_errors

    @property
    def normalized(self) -> dict[str, str | None]:
        return {k: v.normalized_value for k, v in self.fields.items()}

    def apply_business_rules(self, rules: list[dict[str, Any]] | None = None) -> None:
        """执行模板 business_rules，结果累积到 business_errors。"""
        from pdf.validators import validate_business_rules

        self.business_errors.extend(validate_business_rules(self.fields, rules))


class FixedRegionParser:
    """按模板 JSON 的 page/rect 用 PyMuPDF 提取字段。

    模板约定（见 fixed_pdf_exe_tech_stack.md 第 4/23 节）：
      mode 缺省 = fixed；dynamic_fallback 缺省 = true。
    """

    _NORMALIZERS: dict[str, Any] = {
        "string": normalize_text,
        "decimal": normalize_amount,
        "date": normalize_date,
    }

    def parse(self, pdf_path: str, template: dict[str, Any]) -> ParseReport:
        mode = template.get("mode", "fixed")
        if mode != "fixed":
            raise ValueError(f"FixedRegionParser 仅支持 fixed 模板，收到 mode={mode!r}")

        report = ParseReport(template_id=template.get("template", "unknown"), mode=mode)
        with pymupdf.open(pdf_path) as doc:
            for name, spec in template.get("fields", {}).items():
                page_no = int(spec.get("page", 0))
                if page_no >= len(doc):
                    result = FieldResult(name, "")
                    result.fail(f"页码 {page_no} 超出文档范围（共 {len(doc)} 页）")
                    report.fields[name] = result
                    continue

                page = doc[page_no]
                rect = pymupdf.Rect(*spec["rect"])
                raw = page.get_textbox(rect).strip()
                normalizer = self._NORMALIZERS.get(spec.get("type", "string"), normalize_text)
                normalized = normalizer(raw) if raw else None
                if isinstance(normalized, Decimal):
                    normalized = format(normalized, "f")
                result = FieldResult(
                    field_name=name,
                    raw_value=raw,
                    normalized_value=normalized,
                )
                report.fields[name] = validate_field(result, spec)
        report.apply_business_rules(template.get("business_rules"))
        return report
