"""全局视觉主题：QSS 样式表 + 应用图标。

还原 tests/ui.html 设计稿：蓝色主色 #2563eb、slate-900 深色侧边栏、
白色卡片、圆角徽章与按钮。仅使用 QSS 与运行时生成的 QIcon，无外部资源依赖。
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

# ---------------------------------------------------------------- 调色板（对应 ui.html）
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
STYLE_SHEET = f"""
* {{
    font-family: {FONT_STACK};
    outline: none;
}}
QMainWindow, QDialog {{
    background: {BG_BODY};
}}

/* ================ 侧边栏 ================ */
#Sidebar {{
    background: {SIDEBAR_BG};
}}
#BrandLabel {{
    color: #ffffff;
    font-size: 15px;
    font-weight: 600;
    padding: 18px 16px;
}}
#SidebarList {{
    background: {SIDEBAR_BG};
    border: none;
    color: {SIDEBAR_TEXT};
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
    background: {SIDEBAR_HOVER};
    color: #f8fafc;
}}
#SidebarList::item:selected {{
    background: {ACCENT};
    color: #ffffff;
}}

/* ================ 卡片 ================ */
#Card {{
    background: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
#CardTitle {{
    font-size: 14px;
    font-weight: 600;
    color: #0f172a;
}}

/* ================ 按钮 ================ */
QPushButton {{
    border-radius: 6px;
    padding: 6px 14px;
    font-size: 13px;
    font-weight: 500;
}}
QPushButton[cssClass="btn-primary"] {{
    background: {ACCENT};
    color: #ffffff;
    border: none;
}}
QPushButton[cssClass="btn-primary"]:hover {{
    background: {ACCENT_HOVER};
}}
QPushButton[cssClass="btn-primary"]:pressed {{
    background: #1d4ed8;
}}
QPushButton[cssClass="btn-success"] {{
    background: {GREEN};
    color: #ffffff;
    border: none;
}}
QPushButton[cssClass="btn-success"]:hover {{
    background: #15803d;
}}
QPushButton[cssClass="btn-default"] {{
    background: #ffffff;
    color: #475569;
    border: 1px solid #cbd5e1;
}}
QPushButton[cssClass="btn-default"]:hover {{
    border-color: {ACCENT};
    color: {ACCENT};
}}
QPushButton[cssClass="btn-danger-text"] {{
    background: transparent;
    color: {RED};
    border: none;
    padding: 2px 8px;
}}
QPushButton[cssClass="btn-danger-text"]:hover {{
    color: #dc2626;
    text-decoration: underline;
}}
QPushButton:disabled {{
    background: #e2e8f0;
    color: #94a3b8;
    border: none;
}}

/* ================ 拖拽上传区 ================ */
#DropZone {{
    border: 2px dashed #cbd5e1;
    border-radius: 8px;
    background: #f8fafc;
}}
#DropZone[dragOver="true"] {{
    border-color: {ACCENT};
    background: {ACCENT_SOFT};
}}
#DropZoneHint {{
    color: {TEXT_MUTED};
    font-size: 14px;
    background: transparent;
    border: none;
}}

/* ================ 输入控件 ================ */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 6px;
    padding: 6px 12px;
    font-size: 13px;
    color: {TEXT_PRIMARY};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {ACCENT};
}}
QComboBox::drop-down {{
    border: none;
    width: 24px;
}}
QComboBox QAbstractItemView {{
    background: #ffffff;
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT_SOFT};
    selection-color: {TEXT_PRIMARY};
}}

/* ================ 表格 ================ */
QTableWidget {{
    background: #ffffff;
    border: none;
    gridline-color: transparent;
    alternate-background-color: #ffffff;
    selection-background-color: {ACCENT_SOFT};
    selection-color: {TEXT_PRIMARY};
    font-size: 13px;
}}
QTableWidget::item {{
    padding: 8px 12px;
    border-bottom: 1px solid {ROW_BORDER};
}}
QHeaderView::section {{
    background: #f8fafc;
    color: {TEXT_MUTED};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 10px 12px;
    font-size: 12px;
    font-weight: 600;
}}
QTableCornerButton::section {{
    background: #f8fafc;
    border: none;
}}

/* ================ 徽章 ================ */
#badge-success, #badge-info, #badge-warning, #badge-failed {{
    border-radius: 10px;
    padding: 2px 10px;
    font-size: 12px;
    font-weight: 500;
}}
#badge-success {{
    background: {GREEN_BG};
    color: {GREEN_TEXT};
}}
#badge-info {{
    background: {ACCENT_LIGHT};
    color: #1e40af;
}}
#badge-warning {{
    background: {AMBER_BG};
    color: {AMBER_TEXT};
}}
#badge-failed {{
    background: {RED_BG};
    color: {RED_TEXT};
}}
#MutedText {{
    color: {TEXT_MUTED};
    font-size: 12px;
}}
#CellLink {{
    color: {TEXT_PRIMARY};
    font-size: 13px;
    background: transparent;
    border: none;
}}

/* ================ 滚动条 ================ */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: #cbd5e1;
    border-radius: 4px;
    min-height: 32px;
}}
QScrollBar::handle:vertical:hover {{
    background: #94a3b8;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: #cbd5e1;
    border-radius: 4px;
    min-width: 32px;
}}
QScrollBar::handle:horizontal:hover {{
    background: #94a3b8;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

/* ================ 弹窗 / 提示 ================ */
#ModalContainer {{
    background: #ffffff;
    border-radius: 8px;
}}
#Toast {{
    background: {SIDEBAR_BG};
    color: #ffffff;
    border-radius: 6px;
    padding: 10px 16px;
    font-size: 13px;
}}

/* ================ 进度条 ================ */
QProgressBar {{
    background: {BORDER};
    border: none;
    border-radius: 3px;
    height: 6px;
    text-align: center;
}}
QProgressBar::chunk {{
    background: {ACCENT};
    border-radius: 3px;
}}

/* ================ PDF 预览 ================ */
#PdfPreview {{
    background: #475569;
    border-radius: 6px;
}}
"""


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
