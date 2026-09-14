"""V2 状态机（方案 §9 / §10 / §17）：把混在 ``status`` 里的三种语义拆开。

V1 的 ``status`` 同时表达了三件事，导致"人工确认后无法区分机器通过与人
工放行"，也无法表达"处理成功但质量可疑"。

V2 拆成三个正交维度：

    processing_status  pending / processing / completed / needs_ocr / error
    quality_status     valid / warning / invalid / unknown
    review_status      not_required / pending / confirmed / corrected / rejected

约定（方案 §10.2）：**人工确认只改 review_status，不动 quality_status**——
机器当时确实认为有风险，这个结论要留下，否则事后无法审计。

``legacy_status()`` 把三维合成回旧词表（success / manual_review / failed /
warning / needs_ocr）写入 ``documents.status``，兼容既有查询与 UI；
``final_display_status()`` 给出中文展示文案（方案 §10.2 的映射函数）。
"""

from __future__ import annotations

from typing import Any

from pdf.evidence.scorer import (
    CRITICAL_BLOCK,
    CRITICAL_REVIEW,
    DOC_INVALID,
    DOC_VALID,
    FIELD_MEDIUM,
)

# ---------------------------------------------------------------- 处理状态

PROCESSING_PENDING = "pending"
PROCESSING_RUNNING = "processing"
PROCESSING_COMPLETED = "completed"
PROCESSING_NEEDS_OCR = "needs_ocr"
PROCESSING_ERROR = "error"

# ---------------------------------------------------------------- 质量状态

QUALITY_VALID = "valid"
QUALITY_WARNING = "warning"
QUALITY_INVALID = "invalid"
QUALITY_UNKNOWN = "unknown"

# ---------------------------------------------------------------- 复核状态

REVIEW_NOT_REQUIRED = "not_required"
REVIEW_PENDING = "pending"
REVIEW_CONFIRMED = "confirmed"
REVIEW_CORRECTED = "corrected"
REVIEW_REJECTED = "rejected"


def determine_processing_status(*, ok: bool, needs_ocr: bool = False) -> str:
    """处理状态：文件打不开 → error；无文本层 → needs_ocr；否则 completed。"""
    if not ok:
        return PROCESSING_ERROR
    if needs_ocr:
        return PROCESSING_NEEDS_OCR
    return PROCESSING_COMPLETED


def determine_quality_status(
    *,
    critical_invalid: bool = False,
    critical_min: float = 1.0,
    required_low: bool = False,
    document_score: float = 1.0,
    identify_weak: bool = False,
    has_flags: bool = False,
) -> str:
    """质量状态（方案 §17.2 / §18）——临界字段不吃平均值。

    顺序：
      · critical 字段无效或低于 CRITICAL_BLOCK（0.65）→ invalid（不允许 success）；
      · 文档质量分低于 DOC_INVALID（0.75）→ invalid；
      · critical 低于 CRITICAL_REVIEW（0.80）/ 必填字段缺失或低可信 /
        文档分偏低 / **识别不确定**（通用兜底模板）/ **存在结构或业务风险信号**
        （结构异常、业务尾差、双引擎不一致）→ warning（转人工）；
      · 否则 valid。

    宁可多转人工，也不要"解析成功但值其实错了"（方案 §1）。
    """
    if critical_invalid or critical_min < CRITICAL_BLOCK:
        return QUALITY_INVALID
    if document_score < DOC_INVALID:
        return QUALITY_INVALID
    if (
        required_low
        or critical_min < CRITICAL_REVIEW
        or document_score < DOC_VALID
        or identify_weak
        or has_flags
    ):
        return QUALITY_WARNING
    return QUALITY_VALID


def determine_review_status(quality_status: str) -> str:
    """复核状态：质量合格无需复核，其余排队等待人工。"""
    return REVIEW_NOT_REQUIRED if quality_status == QUALITY_VALID else REVIEW_PENDING


def legacy_status(
    *,
    processing_status: str,
    quality_status: str,
    review_status: str = REVIEW_NOT_REQUIRED,
) -> str:
    """三维 → 旧词表（写入 ``documents.status``，供既有查询/UI 使用）。"""
    if processing_status == PROCESSING_NEEDS_OCR:
        return "needs_ocr"
    if processing_status == PROCESSING_ERROR:
        return "failed"
    if processing_status != PROCESSING_COMPLETED:
        return "pending"
    if quality_status == QUALITY_INVALID:
        return "failed"
    if review_status in (REVIEW_CONFIRMED, REVIEW_CORRECTED):
        # 人工已放行：展示层视为通过（quality_status 仍保留机器结论）
        return "success"
    if quality_status == QUALITY_VALID:
        return "success"
    return "manual_review"


def final_display_status(doc: Any) -> str:
    """详情/列表展示用的中文状态（方案 §10.2）。"""
    processing = getattr(doc, "processing_status", None) or "completed"
    quality = getattr(doc, "quality_status", None) or "unknown"
    review = getattr(doc, "review_status", None) or REVIEW_NOT_REQUIRED

    if processing == PROCESSING_NEEDS_OCR:
        return "等待 OCR"
    if processing == PROCESSING_ERROR:
        return "解析失败"
    if quality == QUALITY_INVALID:
        return "数据异常"
    if review == REVIEW_CORRECTED:
        return "人工已修正"
    if review == REVIEW_CONFIRMED:
        return "人工已确认"
    if review == REVIEW_REJECTED:
        return "已驳回"
    if review == REVIEW_PENDING:
        return "待复核"
    return "准确"
