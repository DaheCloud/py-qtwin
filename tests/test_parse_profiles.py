"""解析范围回归：简化模式跳过明细，合计校验与详细重导入仍有效。"""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication

from database.db import get_engine, init_db, make_session_factory
from models.document import AuditLog
from pdf.parse_profiles import PARSE_DETAILED, PARSE_SIMPLE
from pdf.template_engine import TemplateEngine
from services.pdf_service import PdfService
from tests.test_invoice_multiline import _make_invoice
from ui.pages.upload_page import UploadPage, _ROW_ID, _ROW_PROFILE, COL_STATUS, _ParseWorker


def test_simple_skips_table_and_detailed_reimport_restores_items(tmp_path, monkeypatch):
    pdf = tmp_path / "invoice.pdf"
    _make_invoice(pdf, rows=3)
    service = PdfService(TemplateEngine(Path(__file__).resolve().parents[1] / "templates"))
    engine = get_engine(":memory:")
    init_db(engine)
    with make_session_factory(engine)() as session:
        with monkeypatch.context() as patch:
            def unexpected_table(*args, **kwargs):
                pytest.fail("简化解析不应启动表格重建")

            patch.setattr("pdf.dynamic_parser.extract_table_multipage", unexpected_table)
            doc = service.process_document(
                session, str(pdf), parse_profile=PARSE_SIMPLE, cross_verify=True
            )
        values = {field.field_name: field.normalized_value for field in doc.fields}
        assert values["invoice_no"] == "26942000000871475416"
        assert values["buyer_name"] == "中磐建设集团有限公司"
        assert values["amount"] == "221799.60"
        assert values["tax_amount"] == "6654.00"
        assert values["total_amount"] == "228453.60"
        assert "quantity" not in values and "item_name" not in values
        assert "construction_site" not in values and "project_name" not in values
        assert not doc.items
        assert doc.status == "success", doc.error_reason
        assert doc.verifications and all(v.matched for v in doc.verifications)
        log = session.query(AuditLog).filter_by(document_id=doc.id, action="process").one()
        audit = json.loads(log.detail.split(" ", 1)[1])
        assert audit["parse"]["profile"] == PARSE_SIMPLE
        assert audit["structure"] == []
        assert [check["rule"] for check in audit["business"]] == ["grand_total"]
        assert audit["business"][0]["passed"]

        detailed = service.process_document(
            session, str(pdf), force=True, parse_profile=PARSE_DETAILED
        )
        assert len(detailed.items) == 3
        assert "quantity" in {field.field_name for field in detailed.fields}


def test_upload_captures_mode_when_enqueued(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(UploadPage, "_start_worker", lambda self: None)
    monkeypatch.setattr(UploadPage, "_split_duplicates", lambda self, paths: (paths, []))
    page = UploadPage(str(tmp_path / "app.db"))
    worker = Mock()
    page._worker = worker
    try:
        assert page._parse_profile.currentData() == PARSE_SIMPLE
        page.handle_paths([str(tmp_path / "simple.pdf")])
        page._parse_profile.setCurrentIndex(1)
        page.handle_paths([str(tmp_path / "detailed.pdf")])
        page._parse_profile.setCurrentIndex(0)
        assert page._table.item(0, 0).data(_ROW_PROFILE) == PARSE_SIMPLE
        assert page._table.rowCount() == 2
        assert page._table.item(1, COL_STATUS).text() == "等待上传"
        for _ in range(10):
            page._tick_progress()
        assert page._table.item(0, COL_STATUS).text() == "等待解析"
        first_id = page._table.item(0, 0).data(_ROW_ID)
        page._on_parse_started(first_id)
        assert "正在解析：simple.pdf（简化解析）" in page._parse_activity.text()
        assert "等待上传 1 个" in page._parse_activity.text()
        page._on_parse_done(first_id, "failed", "测试失败", 0)
        assert page._table.item(1, 0).data(_ROW_PROFILE) == PARSE_DETAILED
        for _ in range(10):
            page._tick_progress()
        assert [call.args[3] for call in worker.submit.call_args_list] == [
            PARSE_SIMPLE, PARSE_DETAILED
        ]
        second_id = page._table.item(1, 0).data(_ROW_ID)
        page._on_parse_started(second_id)
        assert "正在解析：detailed.pdf（详细解析）" in page._parse_activity.text()
        page._on_parse_done(second_id, "success", "", 1)
        assert "队列已处理完成" in page._parse_activity.text()
        queue_worker = _ParseWorker("unused.db")
        queue_worker.submit("row", "invoice.pdf", True, PARSE_DETAILED)
        assert queue_worker._tasks.get_nowait() == ("row", "invoice.pdf", True, PARSE_DETAILED)
    finally:
        page._timer.stop()
        page.shutdown()
        page.close()
        app.processEvents()


def test_upload_batches_wait_for_all_three(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(UploadPage, "_start_worker", lambda self: None)
    monkeypatch.setattr(UploadPage, "_split_duplicates", lambda self, paths: (paths, []))
    page = UploadPage(str(tmp_path / "batch.db"))
    page._worker = Mock()
    try:
        page.handle_paths([str(tmp_path / f"{i}.pdf") for i in range(9)])
        assert page._table.rowCount() == 9
        assert all(page._table.item(row, COL_STATUS).text() == "等待上传" for row in range(3, 9))
        page._remove_row(page._table.item(8, 0).data(_ROW_ID))
        assert page._table.rowCount() == 8
        assert len(page._pending_uploads) == 5
        for _ in range(10):
            page._tick_progress()
        assert page._worker.submit.call_count == 3
        assert all(page._progress_of(row) == 0 for row in range(3, 8))
        ids = [page._table.item(row, 0).data(_ROW_ID) for row in range(3)]
        page._on_parse_done(ids[0], "failed", "失败", 0)
        page._on_parse_done(ids[1], "success", "", 1)
        assert page._table.rowCount() == 8
        page._on_parse_done(ids[2], "manual_review", "待复核", 2)
        assert page._table.rowCount() == 8
        assert len(page._pending_uploads) == 2
        # 第二批仍处于上传准备，可取消；必须全部结束后再放行末批。
        ids = [page._table.item(row, 0).data(_ROW_ID) for row in range(3, 6)]
        page._remove_row(ids[0])
        page._remove_row(ids[1])
        assert len(page._pending_uploads) == 2
        page._remove_row(ids[2])
        assert not page._pending_uploads
        assert len(page._active_batch) == 2
        assert page._timer.isActive()
        for row_id in list(page._active_batch):
            page._on_parse_done(row_id, "success", "", 3)
        assert not page._active_batch
        assert "队列已处理完成" in page._parse_activity.text()
    finally:
        page._timer.stop()
        page.shutdown()
        page.close()
        app.processEvents()
