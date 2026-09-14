"""独立文本引擎对比证据。"""

from __future__ import annotations

from typing import Any

from pdf.evidence.base import EVIDENCE_CROSS_ENGINE, EvidenceItem, evidence, skipped


def cross_engine_evidence(outcome: Any | None) -> EvidenceItem:
    if outcome is None:
        return skipped(EVIDENCE_CROSS_ENGINE, "第二文本引擎未执行或字段不适用")
    if bool(getattr(outcome, "matched", False)):
        return evidence(EVIDENCE_CROSS_ENGINE, 1.0, "两个文本引擎结果一致")
    secondary = getattr(outcome, "secondary_value", None)
    score = 0.25 if secondary not in (None, "") else 0.0
    return evidence(EVIDENCE_CROSS_ENGINE, score, "两个文本引擎结果不一致")
