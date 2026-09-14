"""V2 证据包（方案 §5 / §6 / §14）。

每个模块负责一个证据维度，输入统一是「字段解析结果 + 模板字段配置」，
输出统一是 :class:`EvidenceItem`（``score=None`` 表示该维度不适用）。

    anchor        锚点是否命中、候选是否唯一
    geometry      取值位置是否符合版式预期（area / x_min / x_max / page）
    region        Region 是否解析、是否发生回退及回退范围
    pattern       type / pattern 是否成立
    cross_engine  PyMuPDF 与 pdfplumber 是否一致
    business      业务数学关系是否成立

scorer 负责融合：只对"适用"的维度按重新归一化的权重加权，
再把兜底/多候选作为乘性惩罚，最后给出字段置信度与文档质量分。
"""

from __future__ import annotations

from pdf.evidence.anchor import anchor_evidence
from pdf.evidence.base import (
    EVIDENCE_ANCHOR,
    EVIDENCE_BUSINESS,
    EVIDENCE_CROSS_ENGINE,
    EVIDENCE_GEOMETRY,
    EVIDENCE_PATTERN,
    EVIDENCE_REGION,
    EVIDENCE_SEMANTIC,
    EVIDENCE_TABLE,
    EvidenceItem,
    evidence,
    skipped,
)
from pdf.evidence.business import business_evidence
from pdf.evidence.cross_engine import cross_engine_evidence
from pdf.evidence.geometry import geometry_evidence
from pdf.evidence.pattern import pattern_evidence
from pdf.evidence.region import region_evidence
from pdf.evidence.scorer import (
    AMBIGUITY_PENALTY,
    CRITICAL_BLOCK,
    CRITICAL_REVIEW,
    DOC_INVALID,
    DOC_VALID,
    EVIDENCE_WEIGHTS,
    FALLBACK_PENALTY,
    FIELD_HIGH,
    FIELD_MEDIUM,
    FieldScore,
    apply_penalties,
    document_score,
    fuse,
    score_field,
    weak_dimensions,
)

__all__ = [
    "AMBIGUITY_PENALTY",
    "CRITICAL_BLOCK",
    "CRITICAL_REVIEW",
    "DOC_INVALID",
    "DOC_VALID",
    "EVIDENCE_ANCHOR",
    "EVIDENCE_BUSINESS",
    "EVIDENCE_CROSS_ENGINE",
    "EVIDENCE_GEOMETRY",
    "EVIDENCE_PATTERN",
    "EVIDENCE_REGION",
    "EVIDENCE_SEMANTIC",
    "EVIDENCE_TABLE",
    "EVIDENCE_WEIGHTS",
    "FALLBACK_PENALTY",
    "FIELD_HIGH",
    "FIELD_MEDIUM",
    "EvidenceItem",
    "FieldScore",
    "anchor_evidence",
    "apply_penalties",
    "business_evidence",
    "cross_engine_evidence",
    "document_score",
    "evidence",
    "fuse",
    "geometry_evidence",
    "pattern_evidence",
    "region_evidence",
    "score_field",
    "skipped",
    "weak_dimensions",
]
