r"""页面 3：系统设置（对应 ui.html 的 #page-settings）。

置信度预警阈值、动态兜底、界面主题等配置，QSettings 持久化为 INI 文件
（%APPDATA%\PdfDataTool\settings.ini，不写 Windows 注册表）。
"""

from __future__ import annotations

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ui.styles import apply_theme, ui_font
from ui.widgets.common import Toast

ORG = "PdfDataTool"
APP = "settings"
KEY_THRESHOLD = "recognition/confidence_threshold"
KEY_FALLBACK = "recognition/dynamic_fallback"
KEY_THEME = "appearance/theme"

THEME_OPTIONS: list[tuple[str, str]] = [
    ("system", "跟随系统"),
    ("light", "浅色"),
    ("dark", "深色"),
]
_THEME_KEYS = {key for key, _ in THEME_OPTIONS}
_THEME_LABEL = dict(THEME_OPTIONS)


def load_settings() -> QSettings:
    """INI 文件后端：%APPDATA%\\PdfDataTool\\settings.ini（与数据库同目录，不写注册表）。"""
    return QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, ORG, APP)


def get_confidence_threshold(settings: QSettings | None = None) -> int:
    settings = settings or load_settings()
    try:
        return int(settings.value(KEY_THRESHOLD, 85))
    except (TypeError, ValueError):
        return 85


def get_theme_choice(settings: QSettings | None = None) -> str:
    """读取主题选择（system / light / dark），默认跟随系统。"""
    settings = settings or load_settings()
    value = str(settings.value(KEY_THEME, "system"))
    return value if value in _THEME_KEYS else "system"


def _divider() -> QFrame:
    line = QFrame()
    line.setFixedHeight(1)
    line.setObjectName("Divider")
    line.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    return line


class SettingsPage(QWidget):
    """系统设置：外观与主题 + 识别与自动化配置。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)

        # ---- 外观与主题卡片
        theme_card = QFrame()
        theme_card.setObjectName("Card")
        theme_l = QVBoxLayout(theme_card)
        theme_l.setContentsMargins(20, 20, 20, 20)
        theme_l.setSpacing(16)

        theme_title = QLabel("外观与主题")
        theme_title.setObjectName("CardTitle")
        theme_title.setFont(ui_font(11, 600))
        theme_l.addWidget(theme_title)
        theme_l.addWidget(_divider())

        theme_label = QLabel("界面主题")
        theme_label.setFont(ui_font(10, 500))
        theme_l.addWidget(theme_label)

        self._theme_combo = QComboBox()
        for key, text in THEME_OPTIONS:
            self._theme_combo.addItem(text, key)
        self._theme_combo.setCurrentIndex(
            THEME_OPTIONS.index(next(o for o in THEME_OPTIONS if o[0] == get_theme_choice()))
        )
        # 连接信号前已完成填充，避免构造期触发切换
        self._theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        theme_l.addWidget(self._theme_combo)

        theme_hint = QLabel("跟随系统：自动匹配 Windows 深浅色模式，系统切换时实时生效")
        theme_hint.setObjectName("MutedText")
        theme_hint.setWordWrap(True)
        theme_l.addWidget(theme_hint)

        root.addWidget(theme_card)

        # ---- 识别与自动化配置卡片
        card = QFrame()
        card.setObjectName("Card")
        card_l = QVBoxLayout(card)
        card_l.setContentsMargins(20, 20, 20, 20)
        card_l.setSpacing(16)

        title = QLabel("识别与自动化配置")
        title.setObjectName("CardTitle")
        title.setFont(ui_font(11, 600))
        card_l.addWidget(title)
        card_l.addWidget(_divider())

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

    # ------------------------------------------------------------ 私有

    def _on_theme_changed(self, index: int) -> None:
        choice = self._theme_combo.itemData(index)
        if not choice:
            return
        load_settings().setValue(KEY_THEME, choice)
        scheme = choice
        app = QApplication.instance()
        if app is not None:
            scheme = apply_theme(app, choice)
        self._toast.show_message(f"主题已应用：{_THEME_LABEL.get(choice, choice)}（{scheme}）")

    def _save(self) -> None:
        settings = load_settings()
        settings.setValue(KEY_THRESHOLD, self._threshold.value())
        settings.setValue(KEY_FALLBACK, self._fallback.isChecked())
        self._toast.show_message("设置已保存")
