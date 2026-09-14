"""文档详情弹窗回归测试。"""

from __future__ import annotations

import pytest


@pytest.fixture
def qt_app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_pdf_preview_renders_one_page_at_a_time(qt_app, tmp_path):
    """多页 PDF 不应在打开详情时一次性布局和渲染全部页面。"""
    from PySide6.QtPdfWidgets import QPdfView

    from database.db import get_engine, init_db
    from ui.pages.detail_dialog import DetailDialog

    db_path = tmp_path / "app.db"
    init_db(get_engine(db_path))

    dialog = DetailDialog(str(db_path), 999, "missing.pdf")

    assert dialog._pdf_view.pageMode() == QPdfView.PageMode.SinglePage
