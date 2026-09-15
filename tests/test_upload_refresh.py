"""上传通知不应触发隐藏列表的全量同步重建。"""

from unittest.mock import Mock

from PySide6.QtCore import Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from ui import main_window


def test_upload_refresh_is_deferred_to_visible_filter(monkeypatch):
    app = QApplication.instance() or QApplication([])

    class UploadStub(QWidget):
        database_changed = Signal()

        def __init__(self, db_path):
            super().__init__()
            self.shutdown = Mock()

    class FilterStub(QWidget):
        detail_requested = Signal(int, str)

        def __init__(self, db_path):
            super().__init__()
            self.reload = Mock()

    monkeypatch.setattr(main_window, "UploadPage", UploadStub)
    monkeypatch.setattr(main_window, "FilterPage", FilterStub)
    monkeypatch.setattr(main_window, "PdfEditorPage", QWidget)
    monkeypatch.setattr(main_window, "SettingsPage", QWidget)
    window = main_window.MainWindow("unused.db")
    try:
        for _ in range(3):
            window._upload_page.database_changed.emit()
        app.processEvents()
        window._filter_page.reload.assert_not_called()
        assert not window._filter_refresh_timer.isActive()

        window._switch_page("filter")
        window._filter_page.reload.assert_called_once()
        window._filter_page.reload.reset_mock()
        window._filter_refresh_timer.setInterval(10)
        for _ in range(3):
            window._upload_page.database_changed.emit()
        window._filter_page.reload.assert_not_called()
        QTest.qWait(50)
        window._filter_page.reload.assert_called_once()

        window._filter_page.reload.reset_mock()
        window._upload_page.database_changed.emit()
        window._switch_page("upload")
        QTest.qWait(50)
        window._filter_page.reload.assert_not_called()
    finally:
        window.close()
