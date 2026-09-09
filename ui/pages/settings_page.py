"""页面 3：系统设置（对应 ui.html 的 #page-settings）。

置信度预警阈值等配置，QSettings 持久化（注册表/INI，无需外部文件）。
"""

from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ui.styles import ui_font
from ui.widgets.common import Toast

ORG = "pdf-project"
APP = "pdf-structure-recognition"
KEY_THRESHOLD = "recognition/confidence_threshold"
KEY_FALLBACK = "recognition/dynamic_fallback"


def load_settings() -> QSettings:
    return QSettings(ORG, APP)


def get_confidence_threshold(settings: QSettings | None = None) -> int:
    settings = settings or load_settings()
    try:
        return int(settings.value(KEY_THRESHOLD, 85))
    except (TypeError, ValueError):
        return 85


class SettingsPage(QWidget):
    """识别与自动化配置。"""

    settings_saved = object  # 占位注解，实际用 Signal

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        card = QFrame()
        card.setObjectName("Card")
        card.setMaximumWidth(600)
        card_l = QVBoxLayout(card)
        card_l.setContentsMargins(20, 20, 20, 20)
        card_l.setSpacing(16)

        title = QLabel("识别与自动化配置")
        title.setObjectName("CardTitle")
        title.setFont(ui_font(11, 600))
        card_l.addWidget(title)

        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet("background: #e2e8f0;")
        card_l.addWidget(line)

        # 置信度阈值
        threshold_label = QLabel("置信度预警阈值 (%)")
        threshold_label.setFont(ui_font(10, 500))
        card_l.addWidget(threshold_label)

        self._threshold = QSpinBox()
        self._threshold.setRange(0, 100)
        self._threshold.setValue(get_confidence_threshold())
        self._threshold.setSuffix(" %")
        card_l.addWidget(self._threshold)

        # 动态兜底开关
        fallback_label = QLabel("解析失败时自动尝试动态兜底（锚点定位）")
        fallback_label.setFont(ui_font(10, 500))
        card_l.addWidget(fallback_label)

        self._fallback = QCheckBox("启用 dynamic_fallback")
        self._fallback.setChecked(bool(load_settings().value(KEY_FALLBACK, True, type=bool)))
        card_l.addWidget(self._fallback)

        card_l.addStretch(1)

        btn_row = QHBoxLayout()
        save_btn = QPushButton("保存设置")
        save_btn.setProperty("cssClass", "btn-primary")
        save_btn.setFixedWidth(100)
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)
        btn_row.addStretch(1)
        card_l.addLayout(btn_row)

        root.addWidget(card)
        root.addStretch(1)

        self._toast = Toast(self)

    def _save(self) -> None:
        settings = load_settings()
        settings.setValue(KEY_THRESHOLD, self._threshold.value())
        settings.setValue(KEY_FALLBACK, self._fallback.isChecked())
        self._toast.show_message("设置已保存")
