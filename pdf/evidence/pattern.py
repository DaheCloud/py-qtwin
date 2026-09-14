"""字段类型与正则格式证据。"""

from __future__ import annotations

from typing import Any

from pdf.evidence.base import EVIDENCE_PATTERN, EvidenceItem, evidence, skipped


def pattern_evidence(result: Any, spec: dict[str, Any]) -> EvidenceItem:
    if not spec.get("pattern") and not spec.get("type"):
        return skipped(EVIDENCE_PATTERN, "未配置类型或正则")
    value = getattr(result, "normalized_value", None)
    if value is None or str(value) == "":
        return evidence(EVIDENCE_PATTERN, 0.0, "字段为空")
    if not bool(getattr(result, "valid", True)):
        return evidence(EVIDENCE_PATTERN, 0.0, "格式校验失败")
    return evidence(EVIDENCE_PATTERN, 1.0, "类型与格式校验通过")
