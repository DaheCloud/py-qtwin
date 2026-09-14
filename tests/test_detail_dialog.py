"""文档详情弹窗回归测试。"""

from __future__ import annotations

import pytest


@pytest.fixture
def qt_app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_missing_pdf_disables_page_navigation(qt_app, tmp_path):
    """无 PDF 时预览保持可用，且不会启动无意义的翻页操作。"""
    from database.db import get_engine, init_db
    from ui.pages.detail_dialog import DetailDialog

    db_path = tmp_path / "app.db"
    init_db(get_engine(db_path))

    dialog = DetailDialog(str(db_path), 999, "missing.pdf")

    assert dialog._page_count == 0
    assert not dialog._btn_page_prev.isEnabled()
    assert not dialog._btn_page_next.isEnabled()


def test_pdf_page_is_rendered_in_background(qt_app, tmp_path):
    """复杂页面走后台栅格化，并把完成结果安全送回预览控件。"""
    import time

    import pymupdf

    from database.db import get_engine, init_db, make_session_factory
    from models.document import Document
    from ui.pages.detail_dialog import DetailDialog

    pdf_path = tmp_path / "wide-table.pdf"
    pdf = pymupdf.open()
    page = pdf.new_page(width=1200, height=800)
    for row in range(40):
        page.insert_text((20, 20 + row * 18), " | ".join(f"column-{col}" for col in range(20)))
    pdf.save(pdf_path)
    pdf.close()

    db_path = tmp_path / "app.db"
    engine = get_engine(db_path)
    init_db(engine)
    with make_session_factory(engine)() as session:
        document = Document(
            file_name=pdf_path.name,
            file_path=str(pdf_path),
            file_hash="wide-table",
            status="success",
        )
        session.add(document)
        session.commit()
        document_id = document.id

    dialog = DetailDialog(str(db_path), document_id, pdf_path.name)
    deadline = time.monotonic() + 5
    while dialog._pdf_view._image.isNull() and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)

    assert not dialog._pdf_view._image.isNull()
    assert dialog._page_count == 1
    assert dialog._page_index == 0
    assert not dialog._btn_page_prev.isEnabled()
    assert not dialog._btn_page_next.isEnabled()
