"""人工确认（"一键确认"）服务测试：状态流转、复核记录、审计日志与边界情况。

筛选页的「一键确认」与详情弹窗的「确认无误」共用 services/review_service.py，
所以这一层固定住语义即可保证两个入口行为一致。

运行：.venv/Scripts/python.exe -m pytest tests/test_review_service.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.db import get_engine, init_db, make_session_factory
from models.document import AuditLog, Document, VerificationResult
from services.review_service import (
    SUSPECT_STATUSES,
    confirm_document,
    confirm_documents,
    is_suspect,
)


@pytest.fixture
def factory():
    """内存库 + 会话工厂（每次用例独立）。"""
    engine = get_engine(":memory:")
    init_db(engine)
    return make_session_factory(engine)


def _doc(
    session,
    name: str,
    status: str,
    *,
    error_reason: str | None = None,
    verifications: tuple[tuple[str, bool, str], ...] = (),
) -> Document:
    """造一条记录；verifications 项为 (字段名, 双引擎是否一致, 复核状态)。"""
    doc = Document(
        file_name=name,
        file_path=f"C:/{name}",
        file_hash=f"hash-{name}",
        status=status,
        error_reason=error_reason,
    )
    for field_name, matched, review_status in verifications:
        doc.verifications.append(
            VerificationResult(
                field_name=field_name,
                primary_value=f"{field_name}-primary",
                secondary_value=None,
                matched=matched,
                review_status=review_status,
            )
        )
    session.add(doc)
    session.flush()
    return doc


def _seed(factory) -> dict[str, int]:
    """造 4 条记录：可疑 2（manual_review / warning）、准确 1、失败 1。"""
    with factory() as session:
        review = _doc(
            session, "review.pdf", "manual_review",
            error_reason="交叉验证不一致：grand_total",
            verifications=(("grand_total", False, "pending"), ("buyer_name", False, "confirmed")),
        )
        warning = _doc(session, "warning.pdf", "warning", error_reason="通用兜底模板")
        success = _doc(session, "success.pdf", "success")
        failed = _doc(session, "failed.pdf", "failed", error_reason="关键字段缺失")
        session.commit()
        return {
            "review": review.id,
            "warning": warning.id,
            "success": success.id,
            "failed": failed.id,
        }


class TestStatusGate:
    def test_suspect_statuses(self):
        assert is_suspect("manual_review") is True
        assert is_suspect("warning") is True
        assert is_suspect("success") is False
        assert is_suspect("failed") is False
        assert is_suspect(None) is False
        assert set(SUSPECT_STATUSES) == {"manual_review", "warning"}


class TestConfirmDocuments:
    def test_only_suspect_records_are_confirmed(self, factory):
        ids = _seed(factory)

        with factory() as session:
            report = confirm_documents(session, list(ids.values()), source="test")
            session.commit()

        assert report.confirmed == [ids["review"], ids["warning"]]
        assert report.skipped == [(ids["success"], "success"), (ids["failed"], "failed")]
        assert report.missing == []

        with factory() as session:
            statuses = {d.file_name: d.status for d in session.scalars(select(Document))}
        assert statuses == {
            "review.pdf": "success",
            "warning.pdf": "success",
            "success.pdf": "success",
            "failed.pdf": "failed",  # 失败记录不参与一键确认
        }

    def test_confirmed_document_is_cleaned_up(self, factory):
        """error_reason 清空（已人工核对，不再提示），状态变准确。"""
        ids = _seed(factory)

        with factory() as session:
            confirm_documents(session, [ids["review"]], source="test")
            session.commit()

        with factory() as session:
            doc = session.get(Document, ids["review"])
            assert doc.status == "success"
            assert doc.error_reason is None

    def test_verifications_marked_confirmed_with_value_and_time(self, factory):
        ids = _seed(factory)

        with factory() as session:
            report = confirm_documents(session, [ids["review"]], source="test")
            session.commit()
        # 只有 pending 的那条需要确认（已确认的不重复计数）
        assert report.confirmed_fields == 1

        with factory() as session:
            doc = session.get(Document, ids["review"])
            by_name = {v.field_name: v for v in doc.verifications}
            pending = by_name["grand_total"]
            assert pending.review_status == "confirmed"
            assert pending.reviewed_value == "grand_total-primary"
            assert pending.reviewed_at is not None
            # 已人工确认过的记录不被覆盖（保留人工改过的复核值）
            already = by_name["buyer_name"]
            assert already.review_status == "confirmed"

    def test_audit_log_written_with_source(self, factory):
        ids = _seed(factory)

        with factory() as session:
            confirm_documents(session, [ids["review"], ids["warning"]], source="filter_page")
            session.commit()

        with factory() as session:
            logs = list(session.scalars(select(AuditLog).where(AuditLog.action == "manual_confirm")))
        assert {log.document_id for log in logs} == {ids["review"], ids["warning"]}
        assert all("source=filter_page" in (log.detail or "") for log in logs)

    def test_dry_run_reports_without_writing(self, factory):
        ids = _seed(factory)

        with factory() as session:
            report = confirm_documents(session, list(ids.values()), source="test", dry_run=True)
            session.rollback()

        assert report.confirmed_count == 2
        with factory() as session:
            doc = session.get(Document, ids["review"])
            assert doc.status == "manual_review"
            assert session.scalars(select(AuditLog)).all() == []

    def test_second_confirm_is_noop(self, factory):
        """确认过之后状态已是准确 → 再点一键确认会跳过（不会重复写审计）。"""
        ids = _seed(factory)
        with factory() as session:
            confirm_documents(session, [ids["review"]], source="test")
            session.commit()

        with factory() as session:
            again = confirm_documents(session, [ids["review"]], source="test")
            session.commit()

        assert again.confirmed == []
        assert again.skipped == [(ids["review"], "success")]
        with factory() as session:
            logs = session.scalars(select(AuditLog).where(AuditLog.action == "manual_confirm")).all()
        assert len(logs) == 1

    def test_empty_and_missing_ids(self, factory):
        ids = _seed(factory)

        with factory() as session:
            empty = confirm_documents(session, [], source="test")
            missing = confirm_documents(session, [ids["review"], 999999], source="test")
            session.commit()

        assert empty.confirmed == [] and empty.skipped == [] and empty.missing == []
        assert missing.confirmed == [ids["review"]]
        assert missing.missing == [999999]

    def test_duplicate_ids_are_confirmed_once(self, factory):
        ids = _seed(factory)

        with factory() as session:
            report = confirm_documents(session, [ids["review"], ids["review"]], source="test")
            session.commit()

        assert report.confirmed == [ids["review"]]


class TestConfirmDocument:
    def test_direct_confirm_ignores_status_gate(self, factory):
        """详情弹窗的按钮只在可疑状态显示，直接确认不再做状态判断。"""
        ids = _seed(factory)

        with factory() as session:
            doc = session.get(Document, ids["success"])
            marked = confirm_document(session, doc, source="detail_dialog")
            session.commit()

        assert marked == 0  # 准确记录没有待复核字段
        with factory() as session:
            log = session.scalars(select(AuditLog).where(AuditLog.action == "manual_confirm")).one()
        assert log.document_id == ids["success"]
        assert "source=detail_dialog" in (log.detail or "")
