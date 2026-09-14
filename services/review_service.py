"""人工确认（复核）服务：把「可疑/待校验」记录标记为「准确」。

单一事实来源：筛选页的"一键确认"与详情弹窗的"确认无误"都走这里，
保证两条入口的落库语义完全一致（状态、复核记录、审计日志）。

一条记录的确认语义：
  · ``review_status`` → ``confirmed``（V2，方案 §10：**只改复核状态**）
  · ``documents.status`` → success，并清空 ``error_reason``（已人工核对，无需再提示）
  · **``quality_status`` 保持不变**：机器当时确实认为有风险，这个结论要留下，
    否则事后无法区分"机器自动通过"与"人工放行"（方案 §10.2）
  · 未确认的校验记录 → ``review_status="confirmed"``，复核值取 ``primary_value``、
    记录 ``reviewed_at``；已确认的保持原样（不覆盖人工改过的复核值/时间）
  · 写一条 ``manual_confirm`` 审计日志（detail 带来源，便于追溯是哪个入口确认的）

只做"标记确认"，不做数值改写：取值对不对由人工在详情弹窗里核对后再确认。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.document import AuditLog, Document
from pdf.states import REVIEW_CONFIRMED, legacy_status

# 可被人工确认的状态（V1 词表，兼容旧库/旧调用）
SUSPECT_STATUSES: tuple[str, ...] = ("manual_review", "warning")

# V2 复核状态（方案 §9）
REVIEW_PENDING = "pending"
STATUS_SUCCESS = "success"


def is_suspect(status: str | None) -> bool:
    """是否属于「可疑/待校验」（可人工确认）状态（V1：按展示状态判断）。"""
    return status in SUSPECT_STATUSES


def needs_review(doc: Document) -> bool:
    """该记录是否需要人工复核（V2 优先看 ``review_status``）。

    ``review_status`` 为 pending → 需要复核；
    为 confirmed/corrected/rejected → 已处理，不再复核；
    缺失或仍是默认 ``not_required``（V1 时期写入的老行）→ 退回按展示状态判断。
    """
    review = getattr(doc, "review_status", None)
    if review == REVIEW_PENDING:
        return True
    if review in (None, "not_required"):
        return is_suspect(getattr(doc, "status", None))
    return False


@dataclass
class ConfirmReport:
    """一次确认操作的结果（UI 提示与日志用）。"""

    confirmed: list[int] = field(default_factory=list)
    # 被跳过的记录：(id, 原状态)——状态不是「可疑/待校验」，不该被确认
    skipped: list[tuple[int, str]] = field(default_factory=list)
    # 库中已不存在的 id（列表页与库不同步时会出现）
    missing: list[int] = field(default_factory=list)
    # 顺带标记为已确认的校验记录条数
    confirmed_fields: int = 0

    @property
    def confirmed_count(self) -> int:
        return len(self.confirmed)


def confirm_documents(
    session: Session,
    doc_ids: list[int] | tuple[int, ...],
    *,
    source: str = "filter_page",
    only_suspect: bool = True,
    dry_run: bool = False,
) -> ConfirmReport:
    """批量确认记录（按 id）。

    ``only_suspect=True``（默认）时只确认「可疑/待校验」的记录，其余进
    ``skipped``——避免把已经准确、或解析失败（关键字段可能缺失）的记录
    一起标成准确。``dry_run=True`` 只统计不落库，供 UI 先弹确认框。

    调用方负责 ``session.commit()``（与项目里其它写操作一致）。
    """
    report = ConfirmReport()
    if not doc_ids:
        return report

    ids = list(dict.fromkeys(doc_ids))  # 去重且保持顺序
    docs = {doc.id: doc for doc in session.scalars(select(Document).where(Document.id.in_(ids)))}
    for doc_id in ids:
        doc = docs.get(doc_id)
        if doc is None:
            report.missing.append(doc_id)
            continue
        if only_suspect and not needs_review(doc):
            report.skipped.append((doc_id, doc.status))
            continue
        report.confirmed.append(doc_id)
        if dry_run:
            continue
        report.confirmed_fields += confirm_document(session, doc, source=source)
    return report


def confirm_document(session: Session, doc: Document, *, source: str = "manual") -> int:
    """确认单条记录：复核状态 → 已确认 + 校验记录标记确认 + 审计日志。

    注意（方案 §10）：``quality_status`` **不修改**——机器判断留在库里；
    ``status`` 只是展示用合成状态，随复核状态换算为 success。
    返回本次标记为已确认的校验记录条数（已确认的不重复计数）。
    不提交事务，由调用方提交。
    """
    # ORM 新增列的默认值会让“仅写旧 status 的历史/测试记录”看起来像尚未处理；
    # 在确认入口按旧状态补成等价三维状态，再只修改 review_status。
    if doc.processing_status == "pending" and doc.status in SUSPECT_STATUSES:
        doc.processing_status = "completed"
    if doc.quality_status == "unknown" and doc.status in SUSPECT_STATUSES:
        doc.quality_status = "warning"
    doc.review_status = REVIEW_CONFIRMED
    doc.status = legacy_status(
        processing_status=doc.processing_status,
        quality_status=doc.quality_status,
        review_status=doc.review_status,
    )

    now = datetime.now(timezone.utc)
    marked = 0
    for verification in doc.verifications:
        if verification.review_status == "confirmed":
            continue
        verification.review_status = "confirmed"
        verification.reviewed_value = verification.primary_value
        verification.reviewed_at = now
        marked += 1

    session.add(
        AuditLog(
            document_id=doc.id,
            action="manual_confirm",
            detail=(
                f"status={doc.status} source={source} fields={marked} "
                f"quality_status={doc.quality_status} review_status={doc.review_status}"
            ),
        )
    )
    return marked
