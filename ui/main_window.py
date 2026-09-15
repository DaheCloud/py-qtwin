"""主窗口外壳：侧边栏 + 四个页面栈。

页面：文件上传与管理 / PDF 编辑 / 数据筛选与管理 / 系统设置。
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QMainWindow, QStackedWidget

from ui.pages.filter_page import FilterPage
from ui.pages.pdf_editor_page import PdfEditorPage
from ui.pages.settings_page import SettingsPage
from ui.pages.upload_page import UploadPage
from ui.styles import make_app_icon
from ui.widgets.shell import Sidebar


class MainWindow(QMainWindow):
    """管理后台外壳：左侧导航，右侧页面栈。"""

    def __init__(self, db_path: str = "data/app.db") -> None:
        super().__init__()
        self.setWindowTitle("固定结构 PDF 识别管理后台")
        self.setWindowIcon(make_app_icon())
        self.resize(1280, 820)
        self.setMinimumSize(960, 640)
        self._db_path = db_path

        central = QFrame()
        central_layout = QHBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)

        self._sidebar = Sidebar()
        self._sidebar.page_selected.connect(self._switch_page)
        central_layout.addWidget(self._sidebar)

        right = QFrame()
        right_l = QHBoxLayout(right)
        # 对应 ui.html 的 .content-body { padding: 20px }
        right_l.setContentsMargins(20, 20, 20, 20)
        right_l.setSpacing(0)

        self._stack = QStackedWidget()
        self._upload_page = UploadPage(db_path)
        self._editor_page = PdfEditorPage()
        self._filter_page = FilterPage(db_path)
        self._settings_page = SettingsPage()

        self._stack.addWidget(self._upload_page)   # index 0
        self._stack.addWidget(self._editor_page)   # index 1
        self._stack.addWidget(self._filter_page)   # index 2
        self._stack.addWidget(self._settings_page) # index 3
        right_l.addWidget(self._stack, 1)
        central_layout.addWidget(right, 1)
        self.setCentralWidget(central)

        # 页面联动
        self._filter_refresh_timer = QTimer(self)
        self._filter_refresh_timer.setSingleShot(True)
        self._filter_refresh_timer.setInterval(300)
        self._filter_refresh_timer.timeout.connect(self._refresh_visible_filter)
        self._upload_page.database_changed.connect(self._on_database_changed)
        self._filter_page.detail_requested.connect(self._open_detail)

    # ------------------------------------------------------------------

    def _on_database_changed(self) -> None:
        """上传页只更新任务状态，避免隐藏的数据列表重建阻塞界面。"""
        if self._stack.currentWidget() is self._filter_page:
            self._filter_refresh_timer.start()

    def _refresh_visible_filter(self) -> None:
        if self._stack.currentWidget() is self._filter_page:
            self._filter_page.reload()

    def _switch_page(self, key: str) -> None:
        self._filter_refresh_timer.stop()
        index = {"upload": 0, "editor": 1, "filter": 2, "settings": 3}.get(key, 0)
        self._stack.setCurrentIndex(index)
        if key == "filter":
            self._filter_page.reload()

    def _open_detail(self, doc_id: int, file_name: str) -> None:
        from ui.pages.detail_dialog import DetailDialog

        dialog = DetailDialog(self._db_path, doc_id, file_name, self)
        # 人工确认后刷新筛选页，状态徽章即时更新
        dialog.document_confirmed.connect(lambda _doc_id: self._filter_page.reload())
        dialog.exec()

    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """停止后台解析线程；必须在窗口对象被销毁前调用。"""
        self._filter_refresh_timer.stop()
        self._upload_page.shutdown()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.shutdown()
        super().closeEvent(event)
