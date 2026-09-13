"""外壳部件：深色侧边栏导航。

对应 tests/ui.html 的 .sidebar。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ui.styles import ui_font

NAV_ITEMS = [
    ("📂 文件上传与管理", "upload"),
    ("🔍 数据筛选与管理", "filter"),
    ("⚙️ 系统设置", "settings"),
    ("✏️ PDF 编辑", "editor"),
]


class Sidebar(QFrame):
    """左侧深色导航栏（宽 220px，对应 .sidebar）。"""

    page_selected = Signal(str)  # page key: upload / filter / settings / editor

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setFixedWidth(220)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        brand = QLabel("📄 PDF 结构识别系统")
        brand.setObjectName("BrandLabel")
        brand.setFont(ui_font(11, 600))
        layout.addWidget(brand)

        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet("background: #1e293b;")
        layout.addWidget(line)

        self._list = QListWidget()
        self._list.setObjectName("SidebarList")
        self._list.setFont(ui_font(10))
        for text, key in NAV_ITEMS:
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self._list.addItem(item)
        self._list.setCurrentRow(0)
        self._list.currentRowChanged.connect(self._on_row_changed)
        layout.addWidget(self._list, 1)

    def _on_row_changed(self, row: int) -> None:
        if row >= 0:
            self.page_selected.emit(NAV_ITEMS[row][1])

    def select_page(self, key: str) -> None:
        for row, (_, item_key) in enumerate(NAV_ITEMS):
            if item_key == key:
                self._list.setCurrentRow(row)
                return
