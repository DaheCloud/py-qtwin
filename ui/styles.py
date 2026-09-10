"""全局视觉主题：QSS 样式表（浅色 / 深色 / 跟随系统）+ 应用图标。

还原 tests/ui.html 设计稿：蓝色主色 #2563eb、slate-900 深色侧边栏、
白色卡片、圆角徽章与按钮。仅使用 QSS 与运行时生成的 QIcon，无外部资源依赖。

主题体系：
- 所有颜色以 token（字符串键）管理，LIGHT / DARK 两套取值；
- build_stylesheet(scheme) 生成对应 QSS；
- apply_theme(app, choice) 应用主题（choice: light / dark / system），
  "system" 时跟随操作系统深浅色并监听 colorSchemeChanged 动态切换；
- 自绘控件（delegate 等）通过 token(name) 运行时取当前主题颜色。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPixmap,
    QPen,
)

# ---------------------------------------------------------------- 调色板（浅色 = 默认，模块级常量保持既有 from-import 兼容）
SIDEBAR_BG = "#0f172a"      # slate-900：侧边栏
SIDEBAR_TEXT = "#94a3b8"    # slate-400
SIDEBAR_HOVER = "#1e293b"   # slate-800
ACCENT = "#2563eb"          # blue-600：主按钮 / 选中态
ACCENT_HOVER = "#3b82f6"    # blue-500
ACCENT_LIGHT = "#dbeafe"    # blue-100：badge-info 背景
ACCENT_SOFT = "#eff6ff"     # blue-50：hover 背景
BG_BODY = "#f1f5f9"         # slate-100：页面底色
BG_CARD = "#ffffff"         # 卡片
BORDER = "#e2e8f0"          # slate-200
ROW_BORDER = "#f1f5f9"      # slate-100：行分隔线
TEXT_PRIMARY = "#334155"    # slate-700
TEXT_MUTED = "#64748b"      # slate-500
TEXT_FAINT = "#94a3b8"

# 状态色
GREEN = "#16a34a"           # btn-success / badge-success 文字
GREEN_BG = "#dcfce7"        # badge-success 背景
GREEN_TEXT = "#15803d"
AMBER_BG = "#fef3c7"        # badge-warning 背景
AMBER_TEXT = "#b45309"
RED = "#ef4444"             # btn-danger-text / badge-failed
RED_BG = "#fee2e2"
RED_TEXT = "#b91c1c"

# ---------------------------------------------------------------- 主题 token（双主题完整取值）
LIGHT: dict[str, str] = {
    "SIDEBAR_BG": "#0f172a",
    "SIDEBAR_TEXT": "#94a3b8",
    "SIDEBAR_HOVER": "#1e293b",
    "ACCENT": "#2563eb",
    "ACCENT_HOVER": "#3b82f6",
    "ACCENT_SOFT": "#eff6ff",       # 选中 / hover 背景
    "ACCENT_LIGHT": "#dbeafe",      # badge-info 背景
    "INFO_TEXT": "#1e40af",         # badge-info 文字
    "BG_BODY": "#f1f5f9",           # 页面底色
    "BG_CARD": "#ffffff",           # 卡片 / 弹窗
    "BG_SUBTLE": "#f8fafc",         # 表头 / 拖拽区底色
    "BG_INPUT": "#ffffff",          # 输入控件背景
    "BORDER": "#e2e8f0",            # 通用边框 / 分隔线
    "BORDER_INPUT": "#cbd5e1",      # 输入框 / 默认按钮边框
    "ROW_BORDER": "#f1f5f9",        # 行分隔线
    "TITLE_TEXT": "#0f172a",        # 卡片标题
    "TEXT_PRIMARY": "#334155",
    "TEXT_MUTED": "#64748b",
    "TEXT_FAINT": "#94a3b8",
    "BTN_DEFAULT_TEXT": "#475569",
    "DISABLED_BG": "#e2e8f0",
    "DISABLED_TEXT": "#94a3b8",
    "SCROLL_HANDLE": "#94a3b8",
    "SCROLL_HANDLE_HOVER": "#64748b",
    "GREEN": "#16a34a",
    "GREEN_BG": "#dcfce7",
    "GREEN_TEXT": "#15803d",
    "AMBER_BG": "#fef3c7",
    "AMBER_TEXT": "#b45309",
    "RED": "#ef4444",
    "RED_BG": "#fee2e2",
    "RED_TEXT": "#b91c1c",
    "DANGER_TIP_BG": "#fef2f2",     # 错误原因提示框
    "DANGER_TIP_BORDER": "#fecaca",
}

DARK: dict[str, str] = {
    "SIDEBAR_BG": "#020617",        # slate-950：比页面更深
    "SIDEBAR_TEXT": "#94a3b8",
    "SIDEBAR_HOVER": "#1e293b",
    "ACCENT": "#2563eb",
    "ACCENT_HOVER": "#3b82f6",
    "ACCENT_SOFT": "#1e3a8a",       # blue-900
    "ACCENT_LIGHT": "#1e3a8a",
    "INFO_TEXT": "#93c5fd",         # blue-300
    "BG_BODY": "#0f172a",
    "BG_CARD": "#1e293b",           # slate-800
    "BG_SUBTLE": "#223046",
    "BG_INPUT": "#0b1220",
    "BORDER": "#334155",            # slate-700
    "BORDER_INPUT": "#475569",
    "ROW_BORDER": "#263349",
    "TITLE_TEXT": "#f1f5f9",
    "TEXT_PRIMARY": "#e2e8f0",      # slate-200
    "TEXT_MUTED": "#94a3b8",
    "TEXT_FAINT": "#64748b",
    "BTN_DEFAULT_TEXT": "#cbd5e1",
    "DISABLED_BG": "#334155",
    "DISABLED_TEXT": "#64748b",
    "SCROLL_HANDLE": "#475569",
    "SCROLL_HANDLE_HOVER": "#64748b",
    "GREEN": "#16a34a",
    "GREEN_BG": "#14532d",          # green-900
    "GREEN_TEXT": "#4ade80",        # green-400
    "AMBER_BG": "#78350f",          # amber-900
    "AMBER_TEXT": "#fcd34d",        # amber-300
    "RED": "#ef4444",
    "RED_BG": "#7f1d1d",            # red-900
    "RED_TEXT": "#fca5a5",          # red-300
    "DANGER_TIP_BG": "#450a0a",     # red-950
    "DANGER_TIP_BORDER": "#7f1d1d",
}

THEMES: dict[str, dict[str, str]] = {"light": LIGHT, "dark": DARK}

# ---------------------------------------------------------------- 运行时主题状态
_theme_choice = "system"    # 用户选择：light / dark / system
_current_scheme = "light"   # 实际生效：light / dark
_scheme_connected = False   # colorSchemeChanged 是否已连接


def current_scheme() -> str:
    """当前实际生效的主题（light / dark）。"""
    return _current_scheme


def token(name: str) -> str:
    """按当前生效主题取颜色（供 delegate 等运行时自绘使用）。"""
    return THEMES[_current_scheme][name]


def _resolve_scheme(choice: str) -> str:
    """把用户选择解析为实际主题；system 时读取系统深浅色。"""
    if choice == "system":
        from PySide6.QtGui import QGuiApplication

        hints = QGuiApplication.styleHints()
        try:
            return "dark" if hints.colorScheme() == Qt.ColorScheme.Dark else "light"
        except AttributeError:  # 旧版本 Qt 无 colorScheme API
            return "light"
    return choice if choice in ("light", "dark") else "light"


def apply_theme(app, choice: str) -> str:
    """应用主题（choice: light / dark / system），返回实际生效主题。

    "system" 时连接 colorSchemeChanged，跟随系统深浅色动态切换。
    """
    global _theme_choice, _current_scheme, _scheme_connected
    _theme_choice = choice if choice in ("light", "dark", "system") else "system"
    _current_scheme = _resolve_scheme(_theme_choice)

    hints = app.styleHints()
    if _scheme_connected:
        try:
            hints.colorSchemeChanged.disconnect(_on_scheme_changed)
        except (RuntimeError, TypeError):
            pass
        _scheme_connected = False
    if _theme_choice == "system":
        hints.colorSchemeChanged.connect(_on_scheme_changed)
        _scheme_connected = True

    app.setStyleSheet(build_stylesheet(_current_scheme))
    return _current_scheme


def _on_scheme_changed(color_scheme) -> None:
    """系统深浅色变化时（跟随系统模式下）刷新 QSS。"""
    from PySide6.QtWidgets import QApplication

    global _current_scheme
    resolved = _resolve_scheme("system")
    if resolved == _current_scheme:
        return
    app = QApplication.instance()
    if app is not None:
        apply_theme(app, "system")


def _reload_theme() -> None:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is not None:
        apply_theme(app, _theme_choice)


FONT_STACK = '"Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "PingFang SC", sans-serif'

STATUS_BADGE_CLASS = {
    "success": "badge-success",
    "manual_review": "badge-warning",
    "warning": "badge-warning",
    "failed": "badge-failed",
    "processing": "badge-info",
    "pending": "badge-info",
}


# ---------------------------------------------------------------- QSS
def build_stylesheet(scheme: str = "light") -> str:
    """按主题（light / dark）生成全局 QSS。"""
    t = THEMES.get(scheme, LIGHT)
    return f"""
* {{
    font-family: {FONT_STACK};
    outline: none;
}}
QMainWindow, QDialog {{
    background: {t['BG_BODY']};
}}
QLabel, QCheckBox, QRadioButton {{
    color: {t['TEXT_PRIMARY']};
    background: transparent;
}}

/* ================ 侧边栏 ================ */
#Sidebar {{
    background: {t['SIDEBAR_BG']};
}}
#BrandLabel {{
    color: #ffffff;
    font-size: 15px;
    font-weight: 600;
    padding: 18px 16px;
}}
#SidebarList {{
    background: {t['SIDEBAR_BG']};
    border: none;
    color: {t['SIDEBAR_TEXT']};
    font-size: 13px;
    outline: none;
}}
#SidebarList::item {{
    height: 42px;
    margin: 2px 8px;
    border-radius: 6px;
    padding-left: 12px;
}}
#SidebarList::item:hover {{
    background: {t['SIDEBAR_HOVER']};
    color: #f8fafc;
}}
#SidebarList::item:selected {{
    background: {t['ACCENT']};
    color: #ffffff;
}}

/* ================ 卡片 / 分隔线 ================ */
#Card {{
    background: {t['BG_CARD']};
    border: 1px solid {t['BORDER']};
    border-radius: 8px;
}}
#CardTitle {{
    font-size: 14px;
    font-weight: 600;
    color: {t['TITLE_TEXT']};
}}
#Divider {{
    background: {t['BORDER']};
    border: none;
}}
#DialogHead {{
    background: {t['BG_CARD']};
    border-bottom: 1px solid {t['BORDER']};
}}

/* ================ 按钮 ================ */
QPushButton {{
    border-radius: 6px;
    padding: 6px 14px;
    font-size: 13px;
    font-weight: 500;
    color: {t['TEXT_PRIMARY']};
    background: {t['BG_CARD']};
}}
QPushButton[cssClass="btn-primary"] {{
    background: {t['ACCENT']};
    color: #ffffff;
    border: none;
}}
QPushButton[cssClass="btn-primary"]:hover {{
    background: {t['ACCENT_HOVER']};
}}
QPushButton[cssClass="btn-primary"]:pressed {{
    background: #1d4ed8;
}}
QPushButton[cssClass="btn-success"] {{
    background: {t['GREEN']};
    color: #ffffff;
    border: none;
}}
QPushButton[cssClass="btn-success"]:hover {{
    background: #15803d;
}}
QPushButton[cssClass="btn-default"] {{
    background: {t['BG_CARD']};
    color: {t['BTN_DEFAULT_TEXT']};
    border: 1px solid {t['BORDER_INPUT']};
}}
QPushButton[cssClass="btn-default"]:hover {{
    border-color: {t['ACCENT']};
    color: {t['ACCENT']};
}}
QPushButton[cssClass="btn-danger-text"] {{
    background: transparent;
    color: {t['RED']};
    border: none;
    padding: 2px 8px;
}}
QPushButton[cssClass="btn-danger-text"]:hover {{
    color: #dc2626;
    text-decoration: underline;
}}
QPushButton:disabled {{
    background: {t['DISABLED_BG']};
    color: {t['DISABLED_TEXT']};
    border: none;
}}
/* 自适应列宽开关按钮 */
QPushButton[cssClass="btn-auto-fit"] {{
    background: {t['BG_CARD']};
    color: {t['BTN_DEFAULT_TEXT']};
    border: 1px solid {t['BORDER_INPUT']};
}}
QPushButton[cssClass="btn-auto-fit"]:hover {{
    border-color: {t['ACCENT']};
    color: {t['ACCENT']};
}}
QPushButton[cssClass="btn-auto-fit"]:checked {{
    background: {t['ACCENT']};
    color: #ffffff;
    border-color: {t['ACCENT']};
}}
/* 表格行选择框：自绘方框（不依赖系统控件渲染） */
QPushButton[cssClass="row-check"] {{
    background: #ffffff;
    border: 1px solid {t['TEXT_FAINT']};
    border-radius: 4px;
    padding: 0;
    font-size: 12px;
    color: #ffffff;
}}
QPushButton[cssClass="row-check"]:hover {{
    border-color: {t['ACCENT']};
    background: {t['ACCENT_SOFT']};
}}
QPushButton[cssClass="row-check"]:checked {{
    background: {t['ACCENT']};
    border-color: {t['ACCENT']};
    color: #ffffff;
    font-weight: 700;
}}
/* 行勾选的透明按钮：只显示居中自绘图标 */
QPushButton[cssClass="row-check-icon"] {{
    background: transparent;
    border: none;
    padding: 0;
}}

/* ================ 拖拽上传区 ================ */
#DropZone {{
    border: 2px dashed {t['BORDER_INPUT']};
    border-radius: 8px;
    background: {t['BG_SUBTLE']};
}}
#DropZone[dragOver="true"] {{
    border-color: {t['ACCENT']};
    background: {t['ACCENT_SOFT']};
}}
#DropZoneHint {{
    color: {t['TEXT_MUTED']};
    font-size: 14px;
    background: transparent;
    border: none;
}}

/* ================ 输入控件 ================ */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {t['BG_INPUT']};
    border: 1px solid {t['BORDER_INPUT']};
    border-radius: 6px;
    padding: 6px 12px;
    font-size: 13px;
    color: {t['TEXT_PRIMARY']};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {t['ACCENT']};
}}
QComboBox::drop-down {{
    border: none;
    width: 24px;
}}
QComboBox QAbstractItemView {{
    background: {t['BG_CARD']};
    border: 1px solid {t['BORDER']};
    selection-background-color: {t['ACCENT_SOFT']};
    selection-color: {t['TEXT_PRIMARY']};
    color: {t['TEXT_PRIMARY']};
}}

/* ================ 表格 ================ */
QTableWidget {{
    background: {t['BG_CARD']};
    border: none;
    gridline-color: transparent;
    alternate-background-color: {t['BG_CARD']};
    selection-background-color: {t['ACCENT_SOFT']};
    selection-color: {t['TEXT_PRIMARY']};
    color: {t['TEXT_PRIMARY']};
    font-size: 13px;
}}
QTableWidget::item {{
    padding: 8px 12px;
    border-bottom: 1px solid {t['ROW_BORDER']};
}}
QHeaderView::section {{
    background: {t['BG_SUBTLE']};
    color: {t['TEXT_MUTED']};
    border: none;
    border-right: 1px solid {t['BORDER']};
    border-bottom: 1px solid {t['BORDER']};
    padding: 10px 12px;
    font-size: 12px;
    font-weight: 600;
}}
QHeaderView::section:hover {{
    background: {t['ACCENT_SOFT']};
}}
QTableCornerButton::section {{
    background: {t['BG_SUBTLE']};
    border: none;
}}
#ActionTable {{
    border-left: 1px solid {t['BORDER']};
}}

/* ================ 徽章 ================ */
#badge-success, #badge-info, #badge-warning, #badge-failed {{
    border-radius: 10px;
    padding: 2px 10px;
    font-size: 12px;
    font-weight: 500;
}}
#badge-success {{
    background: {t['GREEN_BG']};
    color: {t['GREEN_TEXT']};
}}
#badge-info {{
    background: {t['ACCENT_LIGHT']};
    color: {t['INFO_TEXT']};
}}
#badge-warning {{
    background: {t['AMBER_BG']};
    color: {t['AMBER_TEXT']};
}}
#badge-failed {{
    background: {t['RED_BG']};
    color: {t['RED_TEXT']};
}}
#MutedText {{
    color: {t['TEXT_MUTED']};
    font-size: 12px;
}}
#CellLink {{
    color: {t['TEXT_PRIMARY']};
    font-size: 13px;
    background: transparent;
    border: none;
}}

/* ================ 滚动条 ================ */
QScrollBar:vertical {{
    background: {t['BG_SUBTLE']};
    width: 14px;
    margin: 2px;
    border-radius: 7px;
}}
QScrollBar::handle:vertical {{
    background: {t['SCROLL_HANDLE']};
    border-radius: 5px;
    min-height: 32px;
}}
QScrollBar::handle:vertical:hover {{
    background: {t['SCROLL_HANDLE_HOVER']};
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{
    background: {t['BG_SUBTLE']};
    height: 14px;
    margin: 2px;
    border-radius: 7px;
}}
QScrollBar::handle:horizontal {{
    background: {t['SCROLL_HANDLE']};
    border-radius: 5px;
    min-width: 40px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {t['SCROLL_HANDLE_HOVER']};
}}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

/* ================ 弹窗 / 提示 ================ */
#ModalContainer {{
    background: {t['BG_CARD']};
    border-radius: 8px;
}}
#Toast {{
    background: {t['SIDEBAR_BG']};
    color: #ffffff;
    border-radius: 6px;
    padding: 10px 16px;
    font-size: 13px;
}}

/* ================ 进度条 ================ */
QProgressBar {{
    background: {t['BORDER']};
    border: none;
    border-radius: 3px;
    height: 6px;
    text-align: center;
}}
QProgressBar::chunk {{
    background: {t['ACCENT']};
    border-radius: 3px;
}}

/* ================ PDF 预览 ================ */
#PdfPreview {{
    background: #475569;
    border-radius: 6px;
}}
"""


# 兼容旧引用：默认样式表（浅色）
STYLE_SHEET = build_stylesheet("light")


def ui_font(size: int = 10, weight: int = 400) -> QFont:
    """应用统一字体（含中文字形回退）。weight 传 QFont.Weight 数值。"""
    fam = "Microsoft YaHei UI"
    if fam not in QFontDatabase.families():
        fam = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()
    font = QFont(fam, size)
    font.setWeight(QFont.Weight(weight))
    return font


def make_app_icon() -> QIcon:
    """运行时生成应用图标：蓝色渐变圆角方块 + 白色文档折角 + 勾选线。"""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        s = size

        grad = QLinearGradient(QPointF(0, 0), QPointF(s, s))
        grad.setColorAt(0.0, QColor(ACCENT_HOVER))
        grad.setColorAt(1.0, QColor("#1d4ed8"))
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, s, s), s * 0.22, s * 0.22)
        p.fillPath(path, grad)

        doc_l, doc_t = s * 0.26, s * 0.18
        doc_r, doc_b = s * 0.74, s * 0.82
        fold = s * 0.16
        doc = QPainterPath()
        doc.moveTo(doc_l, doc_t)
        doc.lineTo(doc_r - fold, doc_t)
        doc.lineTo(doc_r, doc_t + fold)
        doc.lineTo(doc_r, doc_b)
        doc.lineTo(doc_l, doc_b)
        doc.closeSubpath()
        p.fillPath(doc, QColor("#ffffff"))
        fold_path = QPainterPath()
        fold_path.moveTo(doc_r - fold, doc_t)
        fold_path.lineTo(doc_r, doc_t + fold)
        fold_path.lineTo(doc_r - fold, doc_t + fold)
        fold_path.closeSubpath()
        p.fillPath(fold_path, QColor(ACCENT_LIGHT))

        pen = QPen(QColor(ACCENT), s * 0.07)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        y_mid = doc_t + (doc_b - doc_t) * 0.52
        p.drawLine(QPointF(doc_l + s * 0.08, y_mid), QPointF(doc_l + s * 0.20, y_mid + s * 0.10))
        p.drawLine(QPointF(doc_l + s * 0.20, y_mid + s * 0.10), QPointF(doc_r - s * 0.08, y_mid - s * 0.10))
        p.end()
        icon.addPixmap(pm)
    return icon
