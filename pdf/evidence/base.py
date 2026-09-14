"""证据维度的统一值对象。"""

from __future__ import annotations

from dataclasses import dataclass

EVIDENCE_ANCHOR = "anchor"
EVIDENCE_GEOMETRY = "geometry"
EVIDENCE_PATTERN = "pattern"
EVIDENCE_REGION = "region"
EVIDENCE_CROSS_ENGINE = "cross_engine"
EVIDENCE_BUSINESS = "business"
EVIDENCE_SEMANTIC = "semantic"
EVIDENCE_TABLE = "table"


@dataclass(frozen=True)
class EvidenceItem:
    name: str
    score: float | None
    detail: str = ""

    @property
    def applies(self) -> bool:
        return self.score is not None


def evidence(name: str, score: float, detail: str = "") -> EvidenceItem:
    return EvidenceItem(name, max(0.0, min(1.0, float(score))), detail)


def skipped(name: str, detail: str = "") -> EvidenceItem:
    return EvidenceItem(name, None, detail)
