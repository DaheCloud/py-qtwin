"""字段参与的业务数学关系证据。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pdf.evidence.base import EVIDENCE_BUSINESS, EvidenceItem, evidence, skipped


def business_evidence(field_name: str, checks: Iterable[Any]) -> EvidenceItem:
    related = [
        check
        for check in checks
        if not bool(getattr(check, "skipped", False))
        and field_name in tuple(getattr(check, "fields", ()) or ())
    ]
    if not related:
        return skipped(EVIDENCE_BUSINESS, "字段没有可执行的业务关系")
    if any(getattr(check, "failed", False) and getattr(check, "severity", "") == "error" for check in related):
        return evidence(EVIDENCE_BUSINESS, 0.0, "关键业务关系不成立")
    if any(getattr(check, "failed", False) for check in related):
        return evidence(EVIDENCE_BUSINESS, 0.4, "业务关系存在偏差")
    return evidence(EVIDENCE_BUSINESS, 1.0, "业务关系校验通过")
