"""锚点命中和候选唯一性证据。"""

from __future__ import annotations

from typing import Any

from pdf.evidence.base import EVIDENCE_ANCHOR, EvidenceItem, evidence, skipped


def anchor_evidence(result: Any, spec: dict[str, Any]) -> EvidenceItem:
    parser = str(getattr(result, "parser", "") or "")
    if getattr(result, "strategy", "") == "table" or parser.endswith("-table"):
        return skipped(EVIDENCE_ANCHOR, "表格候选不使用字段锚点")
    if parser == "pymupdf":
        return skipped(EVIDENCE_ANCHOR, "固定矩形候选不使用字段锚点")
    if not spec.get("anchor"):
        return skipped(EVIDENCE_ANCHOR, "字段未配置锚点")
    hit = getattr(result, "anchor_hit", None)
    if hit is False:
        return evidence(EVIDENCE_ANCHOR, 0.0, "未命中锚点")
    if hit is None:
        return evidence(EVIDENCE_ANCHOR, 0.5, "缺少锚点命中记录")
    count = int(getattr(result, "candidate_count", 0) or 0)
    if count > 1:
        return evidence(EVIDENCE_ANCHOR, max(0.65, 1.0 - 0.08 * (count - 1)), f"命中 {count} 个候选")
    return evidence(EVIDENCE_ANCHOR, 1.0, "锚点命中且候选唯一")
