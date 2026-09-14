"""V2 安全策略、候选证据与三维状态的核心回归。"""

from __future__ import annotations

from pdf.candidates import FieldCandidate, select_best
from pdf.evidence import evidence, fuse, score_field
from pdf.policies import (
    FALLBACK_ANCHOR,
    FALLBACK_NONE,
    field_fallback_policy,
    field_region_policy,
)
from pdf.states import (
    QUALITY_INVALID,
    QUALITY_WARNING,
    REVIEW_CONFIRMED,
    determine_quality_status,
    legacy_status,
)


def test_fallback_is_off_by_default_and_field_can_opt_in():
    assert field_fallback_policy({}, {}) == FALLBACK_NONE
    assert field_fallback_policy({}, {"dynamic_fallback": True}) == FALLBACK_ANCHOR
    assert field_fallback_policy(
        {"dynamic_fallback": True}, {"fallback_policy": "none"}
    ) == FALLBACK_NONE


def test_region_failure_defaults_to_fail_and_field_overrides():
    assert field_region_policy({}, {}) == "fail"
    assert field_region_policy({"region_failure_policy": "fail"}, {"region_failure_policy": "page"}) == "page"


def test_zero_is_a_valid_candidate_and_invalid_value_cannot_win():
    zero = FieldCandidate("tax", normalized_value=0, valid=True, score=0.8)
    invalid = FieldCandidate("tax", normalized_value="999", valid=False, score=1.0)
    best, _ = select_best([invalid, zero])
    assert best is zero


def test_evidence_weights_are_renormalized_and_duplicates_do_not_double_count():
    score, dimensions, _ = fuse([
        evidence("anchor", 1.0),
        evidence("pattern", 0.0),
        evidence("anchor", 0.5),
    ])
    assert dimensions == {"anchor": 0.5, "pattern": 0.0}
    assert score == (0.5 * 0.20) / (0.20 + 0.15)
    assert score_field("unknown", []).score == 0.5


def test_critical_score_cannot_be_hidden_by_document_average():
    assert determine_quality_status(critical_min=0.64, document_score=0.99) == QUALITY_INVALID
    assert determine_quality_status(critical_min=0.79, document_score=0.99) == QUALITY_WARNING


def test_manual_confirmation_does_not_change_machine_quality():
    assert legacy_status(
        processing_status="completed",
        quality_status="warning",
        review_status=REVIEW_CONFIRMED,
    ) == "success"
    assert legacy_status(
        processing_status="completed",
        quality_status="invalid",
        review_status=REVIEW_CONFIRMED,
    ) == "success"
