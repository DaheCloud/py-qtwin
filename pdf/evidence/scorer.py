"""证据融合与评分（方案 §6）：把多维证据合成为字段置信度与文档质量分。

关键规则（方案 §6.3 / §6.4）：
  · 只有"适用"的维度参与加权，权重按参与维度**重新归一化**——
    不存在的证据按 0 分算会把正常字段误判成低可信；
  · 兜底与多候选作为**乘性惩罚**（而不是又一个加权维度），
    它们表达的是"这条路径本身不可靠"，与"证据质量"不是同一层语义；
  · 文档分不让 critical 字段的低分被平均值掩盖：critical_min 占 35%，
    并有硬规则「critical 字段分 < 0.80 不允许 success」（方案 §6.4 / §18）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from pdf.evidence.base import EvidenceItem

# 字段级证据权重（方案 §6.3）
EVIDENCE_WEIGHTS: dict[str, float] = {
    "anchor": 0.20,
    "geometry": 0.20,
    "pattern": 0.15,
    "region": 0.15,
    "cross_engine": 0.15,
    "business": 0.15,
}

# 字段置信度分档（方案 §18）
FIELD_HIGH = 0.90
FIELD_MEDIUM = 0.75
# critical 字段低于此分强制人工复核；低于 CRITICAL_BLOCK 不允许 success（方案 §6.4）
CRITICAL_REVIEW = 0.80
CRITICAL_BLOCK = 0.65

# 文档质量分档（方案 §18）
DOC_VALID = 0.90
DOC_INVALID = 0.75

# 文档分权重（方案 §6.4）
DOC_WEIGHTS: dict[str, float] = {
    "critical_min": 0.35,
    "required_avg": 0.25,
    "table": 0.15,
    "business": 0.15,
    "identify": 0.10,
}

# 惩罚系数（乘性）
FALLBACK_PENALTY = 0.85
AMBIGUITY_PENALTY = 0.92


@dataclass
class FieldScore:
    """一个字段的评分结果（落库 + UI 展示 + 审计）。"""

    field_name: str
    score: float = 0.0
    evidence: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    @property
    def level(self) -> str:
        if self.score >= FIELD_HIGH:
            return "high"
        if self.score >= FIELD_MEDIUM:
            return "medium"
        return "low"

    def as_dict(self) -> dict:
        return {
            "field": self.field_name,
            "score": round(self.score, 4),
            "level": self.level,
            "evidence": {k: round(v, 4) for k, v in self.evidence.items()},
            "reasons": list(self.reasons),
        }


def fuse(items: Iterable[EvidenceItem]) -> tuple[float, dict[str, float], list[str]]:
    """加权融合（只统计适用维度并按参与权重归一化）。

    返回 (score, {维度: 分数}, [不适用维度说明])。
    """
    materialized = list(items)
    # 每个维度只保留一条，避免分数计算重复加权而展示字典只显示最后一条。
    by_name = {item.name: item for item in materialized}
    applicable = [(item.name, float(item.score)) for item in by_name.values() if item.applies]
    notes = [f"{item.name} 不适用（{item.detail}）" for item in by_name.values() if not item.applies]
    if not applicable:
        return 0.5, {}, notes + ["没有可用证据"]

    total_weight = 0.0
    weighted = 0.0
    for name, score in applicable:
        weight = EVIDENCE_WEIGHTS.get(name, 0.0)
        if weight <= 0:
            continue
        total_weight += weight
        weighted += weight * score
    if total_weight <= 0:
        return 0.5, {}, notes + ["没有已配置权重的证据"]
    return weighted / total_weight, {name: score for name, score in applicable}, notes


def apply_penalties(
    score: float,
    *,
    fallback_used: bool = False,
    ambiguous: bool = False,
) -> float:
    """兜底与多候选的乘性惩罚。"""
    if fallback_used:
        score *= FALLBACK_PENALTY
    if ambiguous:
        score *= AMBIGUITY_PENALTY
    return max(0.0, min(1.0, score))


def score_field(
    field_name: str,
    items: Iterable[EvidenceItem],
    *,
    fallback_used: bool = False,
    ambiguous: bool = False,
) -> FieldScore:
    """合成一个字段的最终置信度。"""
    base, evidence_map, notes = fuse(items)
    final = apply_penalties(base, fallback_used=fallback_used, ambiguous=ambiguous)
    reasons = list(notes)
    if fallback_used:
        reasons.append("取值来自兜底路径")
    if ambiguous:
        reasons.append("存在多个候选值")
    return FieldScore(field_name=field_name, score=final, evidence=evidence_map, reasons=reasons)


def weak_dimensions(score: FieldScore, threshold: float = FIELD_MEDIUM) -> list[str]:
    """低分证据维度（UI 用于回答"为什么需要复核"）。"""
    return [name for name, value in score.evidence.items() if value < threshold]


def document_score(
    *,
    critical_min: float,
    required_avg: float,
    table_score: float | None = None,
    business_score: float | None = None,
    identify_score: float = 1.0,
) -> float:
    """文档质量分（方案 §6.4）：缺失维度按剩余权重重新归一化。"""
    parts: list[tuple[float, float]] = [
        (DOC_WEIGHTS["critical_min"], critical_min),
        (DOC_WEIGHTS["required_avg"], required_avg),
        (DOC_WEIGHTS["identify"], identify_score),
    ]
    if table_score is not None:
        parts.append((DOC_WEIGHTS["table"], table_score))
    if business_score is not None:
        parts.append((DOC_WEIGHTS["business"], business_score))

    total = sum(weight for weight, _ in parts)
    if total <= 0:
        return 0.0
    return max(0.0, min(1.0, sum(weight * value for weight, value in parts) / total))
