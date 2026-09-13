"""规则式置信度评分（方案 §10/§31/§32）：不用机器学习，纯扣分规则，便于解释、调试、审计。

三块分数分开保存，因为它们是三个不同的问题：
  identify_confidence   —— "选对模板了吗"（模板识别不确定）
  parse_confidence      —— "字段取准了吗"（解析/结构/业务不确定）
  validation_confidence —— "校验可信吗"（结构/业务/双引擎的独立评分）

最终合成 overall_confidence（识别 30% + 解析 40% + 校验 30%）。
parse_confidence 低于 PARSE_REVIEW_THRESHOLD 会转人工复核（方案 §11.2）。

Lazy Cross Validation（方案 §26-§28）：双引擎（pdfplumber）不再默认执行，
由 need_secondary_engine 按风险触发——普通 PDF 一次解析直接通过，只有低置信、
结构异常、业务异常、字段缺失时才启动第二引擎"解决争议"。
"""

from __future__ import annotations

from typing import Any

# 识别置信度"高"的下限：低于此值说明版式特征弱，parse_confidence 再扣一档
IDENTIFY_HIGH_CONFIDENCE = 90

# 解析置信度低于此值 → 转人工复核
PARSE_REVIEW_THRESHOLD = 70

# Lazy Cross Validation 触发阈值（方案 §27）：任一低于阈值即启动第二引擎
SECONDARY_ENGINE_IDENTIFY_THRESHOLD = 85
SECONDARY_ENGINE_PARSE_THRESHOLD = 90

# 扣分权重（方案 §10.1）
PENALTY_FALLBACK = 20
PENALTY_WEAK_IDENTIFY = 10
PENALTY_CROSS_MISMATCH = 20
PENALTY_STRUCTURE_ISSUE = 15
PENALTY_BUSINESS_FAILURE = 30
PENALTY_REQUIRED_MISSING = 10
PENALTY_OPTIONAL_MISSING = 2
PENALTY_FALLBACK_TEMPLATE = 40  # 通用兜底模板：识别层面的不确定更大

# 校验置信度（validation_confidence）扣分权重
PENALTY_VALIDATION_BUSINESS = 30  # 业务数学 error 级
PENALTY_VALIDATION_WARNING = 15  # 业务 warning / 结构异常
PENALTY_VALIDATION_CROSS = 20  # 双引擎不一致

# overall_confidence 合成权重（识别 / 解析 / 校验）
OVERALL_WEIGHTS = (0.3, 0.4, 0.3)


def identify_confidence(identify: Any) -> int:
    """识别置信度 0~100：专属高置信命中 100，只靠 any 命中 90，兜底 60。

    模板指纹（fingerprint）模式下直接用指纹总分（0~100，方案 §18/§32）。
    """
    fingerprint = getattr(identify, "fingerprint", None)
    if fingerprint and fingerprint.get("total") is not None:
        return _clamp(round(float(fingerprint["total"])))
    mode = getattr(identify, "mode", "none")
    if mode == "none":
        return 0
    if mode == "fallback":
        return max(0, 100 - PENALTY_FALLBACK_TEMPLATE)
    matched = getattr(identify, "matched", {}) or {}
    score = 100
    if not matched.get("must"):
        score -= PENALTY_WEAK_IDENTIFY  # 没配 must 或 must 未命中：版式特征较弱
    return _clamp(score)


def parse_confidence(
    *,
    identify: Any = None,
    cross_mismatch: int = 0,
    structure_issues: int = 0,
    business_failures: int = 0,
    required_missing: int = 0,
    optional_missing: int = 0,
) -> int:
    """解析置信度 0~100：从 100 分按风险项扣分。"""
    score = 100
    if getattr(identify, "mode", "match") == "fallback":
        score -= PENALTY_FALLBACK
    if identify is not None and identify_confidence(identify) < IDENTIFY_HIGH_CONFIDENCE:
        score -= PENALTY_WEAK_IDENTIFY
    score -= PENALTY_CROSS_MISMATCH * cross_mismatch
    score -= PENALTY_STRUCTURE_ISSUE * structure_issues
    score -= PENALTY_BUSINESS_FAILURE * business_failures
    score -= PENALTY_REQUIRED_MISSING * required_missing
    score -= PENALTY_OPTIONAL_MISSING * optional_missing
    return _clamp(score)


def validation_confidence(
    *,
    business_failures: int = 0,
    business_warnings: int = 0,
    structure_issues: int = 0,
    cross_mismatch: int = 0,
) -> int:
    """校验置信度 0~100（方案 §31）：结构/业务/双引擎三类风险的独立评分。"""
    score = 100
    score -= PENALTY_VALIDATION_BUSINESS * business_failures
    score -= PENALTY_VALIDATION_WARNING * (business_warnings + structure_issues)
    score -= PENALTY_VALIDATION_CROSS * cross_mismatch
    return _clamp(score)


def overall_confidence(identify_conf: int, parse_conf: int, validation_conf: int) -> int:
    """综合置信度（方案 §31）：识别 30% + 解析 40% + 校验 30%。"""
    identify_w, parse_w, validation_w = OVERALL_WEIGHTS
    return _clamp(round(identify_conf * identify_w + parse_conf * parse_w + validation_conf * validation_w))


def need_secondary_engine(
    *,
    identify_conf: int = 100,
    parse_conf: int = 100,
    structure_issues: int = 0,
    business_issues: int = 0,
    required_missing: int = 0,
    critical_failed: int = 0,
    table_failed: bool = False,
) -> bool:
    """Lazy Cross Validation 触发判定（方案 §27）。

    高可信（模板识别可靠、字段完整、表格重建成功、校验通过）时直接成功，
    不再默认跑 pdfplumber；第二引擎只在有风险时启动，真正用于"解决争议"。
    """
    if identify_conf < SECONDARY_ENGINE_IDENTIFY_THRESHOLD:
        return True
    if parse_conf < SECONDARY_ENGINE_PARSE_THRESHOLD:
        return True
    if structure_issues or business_issues or required_missing or critical_failed:
        return True
    return table_failed


def _clamp(score: int) -> int:
    return max(0, min(100, score))
