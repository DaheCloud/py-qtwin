"""V2 候选值模型（方案 §11 / §12.2 / §14）。

V1 的解析是「一个字段 → 一个结果」：谁先成功就用谁，其他来源的结果直接丢弃。
V2 改成「一个字段 → 多个候选 → 证据排序 → 选优」：

    FieldCandidate(value="12800", source="anchor",      score=0.68)
    FieldCandidate(value="18200", source="table_total", score=0.96)
    → 选中 18200

候选可由三处产生（按解析顺序）：
  · primary    主解析结果（fixed rect / anchor）
  · fallback   动态兜底结果（fallback_policy 允许时）
  · table      Table Engine 回填的明细字段值

排序只按 score（由 pdf/evidence/scorer.py 融合各维度证据得出），
同分时用 _SOURCE_PRIORITY 兜底（table > primary > fallback，宁缺勿错）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 候选来源权重（同分裁决）：表格整表重建 > 主解析 > 兜底
_SOURCE_PRIORITY: dict[str, int] = {"table": 3, "primary": 2, "fallback": 1}


@dataclass
class FieldCandidate:
    """一个字段的一个候选取值（含来源、位置与证据分）。

    ``source_result`` 指向产生该候选的 FieldResult：候选胜出且与当前
    ``report.fields`` 不是同一个时，直接把该 FieldResult 换回去（方案 §10）。
    """

    field_name: str
    raw_value: str = ""
    normalized_value: Any = None
    page: int | None = None
    rect: tuple[float, float, float, float] | None = None
    parser: str = ""
    strategy: str = "primary"
    evidence: dict[str, float] = field(default_factory=dict)
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    selected: bool = False
    valid: bool = True
    errors: list[str] = field(default_factory=list)
    # V2 证据维度需要的溯源信息（与 FieldResult 同名，便于 evidence 模块鸭子类型）
    anchor_hit: bool | None = None
    region_used: bool = False
    region_fallback: str | None = None
    fallback_used: bool = False
    candidate_count: int = 0
    source_result: Any = None

    @property
    def has_value(self) -> bool:
        """是否有可用取值（候选可比较/可落库的前提）。"""
        return self.valid and self.normalized_value is not None and str(self.normalized_value) != ""

    def as_dict(self) -> dict[str, Any]:
        """审计日志用。"""
        return {
            "field": self.field_name,
            "value": _json_value(self.normalized_value),
            "raw_value": self.raw_value,
            "page": self.page,
            "rect": [round(v, 2) for v in self.rect] if self.rect else None,
            "parser": self.parser,
            "strategy": self.strategy,
            "score": round(self.score, 4),
            "selected": self.selected,
            "valid": self.valid,
            "reasons": list(self.reasons),
            "evidence": {key: round(v, 4) for key, v in self.evidence.items()},
        }


def candidate_from_result(
    name: str,
    result: Any,
    *,
    strategy: str = "primary",
) -> FieldCandidate:
    """FieldResult → FieldCandidate（保留位置与溯源信息用于证据计算）。"""
    return FieldCandidate(
        field_name=name,
        raw_value=result.raw_value or "",
        normalized_value=result.normalized_value,
        page=getattr(result, "page", None),
        rect=getattr(result, "rect", None),
        parser=result.parser,
        strategy=strategy,
        valid=bool(getattr(result, "valid", True)),
        errors=list(getattr(result, "errors", None) or []),
        anchor_hit=getattr(result, "anchor_hit", None),
        region_used=bool(getattr(result, "region_used", False)),
        region_fallback=getattr(result, "region_fallback", None),
        fallback_used=bool(getattr(result, "fallback_used", False)),
        candidate_count=int(getattr(result, "candidate_count", 0) or 0),
        source_result=result,
    )


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def rank_candidates(candidates: list[FieldCandidate]) -> list[FieldCandidate]:
    """按 score 降序（同分按来源优先级）排序，并标记 selected 的唯一性。

    只保证排序不改数据；"选谁"由 :func:`select_best` 决定。
    """
    ordered = sorted(
        candidates,
        key=lambda c: (
            c.has_value,  # 有值的永远优先于无值候选
            round(c.score, 6),
            _SOURCE_PRIORITY.get(c.strategy, 0),
        ),
        reverse=True,
    )
    return ordered


def select_best(
    candidates: list[FieldCandidate],
) -> tuple[FieldCandidate | None, list[FieldCandidate]]:
    """选出最优候选并标记 ``selected``；返回 (best, 排序后的全部候选)。"""
    if not candidates:
        return None, []
    ordered = rank_candidates(candidates)
    best = ordered[0] if ordered[0].has_value else None
    for candidate in ordered:
        candidate.selected = candidate is best
    return best, ordered
