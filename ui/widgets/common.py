"""通用小部件：Toast 提示、徽章、可复制单元格委托、进度条行内条。

对应 tests/ui.html 的 .toast / .badge / .copyable-cell / .progress-bar。
"""

from __future__ import annotations

from PySide6.QtCore import (
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QLabel,
    QProgressBar,
    QStyledItemDelegate,
    QWidget,
)

from ui.styles import ACCENT, BORDER, SIDEBAR_BG, ui_font


class Toast(QLabel):
    """右下角自动消失的提示条（对应 .toast）。

    单实例复用：新消息会取消上一个未触发的隐藏定时器，
    避免批量场景下旧定时器把新提示提前隐藏。
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("Toast")
        self.setFont(ui_font(10))
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)
        self.hide()

    def show_message(self, text: str, msec: int = 2200) -> None:
        self.setText(text)
        self.adjustSize()
        self.move(
            max(8, self.parentWidget().width() - self.width() - 24),
            max(8, self.parentWidget().height() - self.height() - 24),
        )
        self.show()
        self.raise_()
        self._hide_timer.start(msec)  # 重启即取消上一个 pending hide


class Badge(QLabel):
    """圆角状态徽章（对应 .badge-*）。kind: success/info/warning/failed。"""

    def __init__(self, text: str = "", kind: str = "info", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_kind(kind)

    def set_kind(self, kind: str) -> None:
        self.setObjectName(f"badge-{kind}")


class CopyCellDelegate(QStyledItemDelegate):
    """可复制单元格：渲染下划线悬停提示语义，点击时复制文本并回调。"""

    cell_copied = Signal(str)

    def editorEvent(self, event, model, option, index) -> bool:  # noqa: N802
        if event.type() == event.Type.MouseButtonRelease and index.isValid():
            text = (index.data() or "").strip()
            if text and text != "—":
                self._copy(text)
                self.cell_copied.emit(text)
                return True
        return super().editorEvent(event, model, option, index)

    @staticmethod
    def _copy(text: str) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(text)


class BadgeDelegate(QStyledItemDelegate):
    """表格内圆角徽章（对应 .badge-*）；数据取 ItemDataRole.UserRole 存的 kind，
    显示文本取 DisplayRole。"""

    _KINDS = {
        "success": ("#dcfce7", "#15803d"),
        "info": ("#dbeafe", "#1e40af"),
        "warning": ("#fef3c7", "#b45309"),
        "failed": ("#fee2e2", "#b91c1c"),
    }

    def paint(self, painter: QPainter, option, index) -> None:
        kind = index.data(Qt.ItemDataRole.UserRole)
        if kind not in self._KINDS:
            super().paint(painter, option, index)
            return
        bg, fg = self._KINDS[kind]
        text = index.data() or ""
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        f = ui_font(9, 500)
        painter.setFont(f)
        fm = painter.fontMetrics()
        text_w = fm.horizontalAdvance(text)
        pad_x, h = 10, 20
        w = text_w + pad_x * 2
        badge = QRectF(
            option.rect.center().x() - w / 2,
            option.rect.center().y() - h / 2,
            w,
            h,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(bg))
        painter.drawRoundedRect(badge, h / 2, h / 2)
        painter.setPen(QColor(fg))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()


class ProgressDelegate(QStyledItemDelegate):
    """表格内 6px 圆角进度条（对应 .progress-bar-bg / .progress-bar-fill）。"""

    def paint(self, painter: QPainter, option, index) -> None:
        value = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(value, (int, float)):
            super().paint(painter, option, index)
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = option.rect.adjusted(4, option.rect.height() // 2 - 3, -4, -option.rect.height() // 2 + 3)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(BORDER))
        painter.drawRoundedRect(QRectF(rect), 3, 3)
        if value > 0:
            w = rect.width() * min(100, int(value)) / 100.0
            painter.setBrush(QColor(ACCENT))
            painter.drawRoundedRect(QRectF(rect.left(), rect.top(), w, rect.height()), 3, 3)
        painter.restore()
