"""页面：PDF 编辑（新增入口）。

打开 PDF → 在内存中做增量编辑（添加文字 / 水印 / 涂黑遮盖 / 页面旋转、删除、插入）
→ 另存为新的 PDF 文件；**原文件始终保持不变**。

预览与交互基于 QGraphicsView 自绘（scene 坐标 1 单位 = 1 pt），
因此点击定位、拖拽框选都能精确映射到 PDF 坐标；编辑能力由 PyMuPDF 提供。
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap, QTransform
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QGraphicsObject,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ui.styles import token, ui_font
from ui.widgets.common import Toast

PAGE_GAP = 30                 # 页间留白（同时用于绘制页码）
MIN_ZOOM, MAX_ZOOM = 0.2, 5.0
MAX_RENDER_SCALE = 3.0        # 单页位图渲染倍率上限（避免内存爆掉）
UNDO_LIMIT = 30

TOOL_BROWSE, TOOL_TEXT, TOOL_REPLACE, TOOL_REDACT = "browse", "text", "replace", "redact"

TEXT_COLORS: list[tuple[str, tuple[float, float, float]]] = [
    ("黑色", (0.0, 0.0, 0.0)),
    ("红色", (0.84, 0.15, 0.15)),
    ("蓝色", (0.13, 0.35, 0.85)),
    ("绿色", (0.09, 0.60, 0.28)),
    ("灰色", (0.45, 0.45, 0.45)),
]
REDACT_FILLS: list[tuple[str, tuple[float, float, float]]] = [
    ("白色遮盖", (1.0, 1.0, 1.0)),
    ("黑色遮盖", (0.0, 0.0, 0.0)),
]
CJK_FONT = "china-s"           # PyMuPDF 内置简体中文字体

_PAGE_NUMBER_FONT = None       # 页码字体缓存：paint 高频调用，避免反复查字体


def _page_number_font():
    global _PAGE_NUMBER_FONT
    if _PAGE_NUMBER_FONT is None:
        _PAGE_NUMBER_FONT = ui_font(10)
    return _PAGE_NUMBER_FONT


# ---------------------------------------------------------------- 坐标转换
def _visual_point(page, x: float, y: float) -> pymupdf.Point:
    """预览视觉坐标（左上原点）→ 页面内容坐标（页面自带 rotation 时需反旋）。"""
    point = pymupdf.Point(x, y)
    if page.rotation:
        point = point * page.derotation_matrix
    return point


def _visual_rect(page, x0: float, y0: float, x1: float, y1: float) -> pymupdf.Rect:
    """视觉矩形 → 页面内容矩形（取变换后的包围盒）。"""
    rect = pymupdf.Rect(x0, y0, x1, y1)
    if page.rotation:
        rect = rect * page.derotation_matrix
    return rect


def _visual_angle(page, angle: float) -> float:
    """视觉角度 → 内容坐标系角度（实测规律：视觉角 = 参数角 - page.rotation）。"""
    return (angle + page.rotation) % 360


def _insert_text(page, point, text: str, *, fontsize: float, color, rotate: float = 0,
                 morph=None, opacity: float | None = None, fontname: str = CJK_FONT) -> None:
    """插入文字；透明参数按 PyMuPDF 版本兼容降级。"""
    kwargs: dict = {"fontsize": fontsize, "fontname": fontname, "color": color, "rotate": rotate}
    if morph is not None:
        kwargs["morph"] = morph
    if opacity is not None:
        try:
            page.insert_text(point, text, fill_opacity=opacity, **kwargs)
            return
        except TypeError:  # 旧版本无 fill_opacity 参数
            pass
    page.insert_text(point, text, **kwargs)


def _short(text: str, limit: int = 16) -> str:
    """提示语里用的一行截断。"""
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def _clean_span_text(text: str) -> str:
    """部分 PDF 按逐字定位排版，提取时会多出大量空格，这里做保守清理。

    仅当「空格数 ≥ 非空格字符数的一半」且「过半是中日韩字符」时才去空格，
    避免误伤正常的中英文混排。
    """
    non_space = [ch for ch in text if not ch.isspace()]
    spaces = len(text) - len(non_space)
    if not non_space or spaces < len(non_space) * 0.5:
        return text.strip()
    cjk = sum(1 for ch in non_space if ord(ch) > 0x2E80)
    return "".join(non_space) if cjk >= len(non_space) * 0.5 else text.strip()


def _span_color(span) -> tuple[float, float, float]:
    """span["color"] 是 sRGB 整数，转成 0~1 三元组供重写使用。"""
    value = int(span.get("color", 0)) & 0xFFFFFF
    return ((value >> 16) / 255, ((value >> 8) & 0xFF) / 255, (value & 0xFF) / 255)


def _find_text_hit(page, point=None, rect=None):
    """命中测试：返回 (原文, 删除区域, 基线起点, 字号, 颜色, 原字体)。

    两种方式：给 rect 时取该选区内与框相交的字符；只给 point 时取点击处字符所在的整个 span。
    PDF 里没有"可编辑的文本对象"，改字只能「删掉原文字 → 按原位置/字号重写」，
    所以这里一次性给出重写所需的全部原始排版参数；未命中（图片/扫描件）返回 None。
    """
    for block in page.get_text("rawdict").get("blocks", []):
        if block.get("type") != 0:  # 只处理文本块，跳过图片
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                chars = span.get("chars") or []
                if not chars:
                    continue
                if rect is not None:
                    picked = [c for c in chars if pymupdf.Rect(c["bbox"]).intersects(rect)]
                else:
                    picked = chars if any(pymupdf.Rect(c["bbox"]).contains(point) for c in chars) else []
                if not picked:
                    continue
                text = _clean_span_text("".join(c["c"] for c in picked))
                if not text:
                    return None
                bounds = pymupdf.Rect(picked[0]["bbox"])
                for char in picked[1:]:
                    bounds |= pymupdf.Rect(char["bbox"])
                return (
                    text,
                    bounds,
                    pymupdf.Point(picked[0]["origin"]),
                    float(span.get("size", 12.0)),
                    _span_color(span),
                    span.get("font", ""),
                )
    return None


# ---------------------------------------------------------------- 画布
class _PageItem(QGraphicsObject):
    """单页渲染项：item 原点即页面左上角，1 单位 = 1 pt。"""

    def __init__(self, index: int, rect: QRectF, total: int) -> None:
        super().__init__()
        self.index = index
        self._rect = QRectF(rect)
        self._total = total
        self._pixmap: QPixmap | None = None
        self._rendered_scale = 0.0

    def page_rect(self) -> QRectF:
        return QRectF(self._rect)

    def boundingRect(self) -> QRectF:  # noqa: N802
        return QRectF(0, 0, self._rect.width(), self._rect.height() + PAGE_GAP)

    def clear_pixmap(self) -> None:
        self._pixmap = None
        self._rendered_scale = 0.0
        self.update()

    def needs_render(self, scale: float) -> bool:
        if self._pixmap is None or self._rendered_scale <= 0:
            return True
        return abs(self._rendered_scale - scale) / scale > 0.15

    def set_pixmap(self, pixmap: QPixmap, scale: float) -> None:
        self._pixmap = pixmap
        self._rendered_scale = scale
        self.update()

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: N802
        rect = self._rect
        painter.fillRect(rect, QColor("#ffffff"))
        if self._pixmap is not None and not self._pixmap.isNull():
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawPixmap(rect, self._pixmap, QRectF(self._pixmap.rect()))
        pen = QPen(QColor(15, 23, 42, 60))
        pen.setWidth(0)  # cosmetic：任意缩放下始终 1px
        painter.setPen(pen)
        painter.drawRect(rect)
        painter.setPen(QColor("#94a3b8"))
        painter.setFont(_page_number_font())
        painter.drawText(
            QRectF(0, rect.height(), rect.width(), PAGE_GAP),
            Qt.AlignmentFlag.AlignCenter,
            f"{self.index + 1} / {self._total}",
        )


class _PdfCanvas(QGraphicsView):
    """PDF 画布：多页垂直排布、懒渲染、点击定位与框选。"""

    text_clicked = Signal(int, float, float)                     # page, x, y（视觉 pt）
    region_selected = Signal(int, float, float, float, float)    # page, x0, y0, x1, y1
    current_page_changed = Signal(int)
    zoom_changed = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("PdfCanvas")
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setBackgroundBrush(QColor("#475569"))

        self._doc: pymupdf.Document | None = None
        self._items: list[_PageItem] = []
        self._zoom = 1.0
        self._tool = TOOL_BROWSE
        self._current = 0
        self._rubber: QGraphicsRectItem | None = None
        self._press_item: _PageItem | None = None
        self._press_scene: QPointF | None = None

        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(60)
        self._render_timer.timeout.connect(self._render_visible)

        self._page_timer = QTimer(self)
        self._page_timer.setSingleShot(True)
        self._page_timer.setInterval(60)
        self._page_timer.timeout.connect(self._sync_current_page)
        self.verticalScrollBar().valueChanged.connect(self._page_timer.start)

    # ------------------------------------------------------------ 文档

    def load(self, doc) -> None:
        self._doc = doc
        self.rebuild()
        self.verticalScrollBar().setValue(0)

    def rebuild(self) -> None:
        """页数 / 页面尺寸变化后重建场景。"""
        self._scene.clear()
        self._items = []
        self._rubber = None
        if self._doc is None:
            self._scene.setSceneRect(QRectF())
            return
        total = self._doc.page_count
        y = 0.0
        max_w = 0.0
        for i in range(total):
            r = self._doc[i].rect
            item = _PageItem(i, QRectF(0, 0, r.width, r.height), total)
            item.setPos(0, y)
            self._scene.addItem(item)
            self._items.append(item)
            y += r.height + PAGE_GAP
            max_w = max(max_w, r.width)
        self._scene.setSceneRect(QRectF(-20, -20, max_w + 40, y + 20))
        self._schedule_render()
        self._sync_current_page()

    def refresh_page(self, index: int) -> None:
        if 0 <= index < len(self._items):
            self._items[index].clear_pixmap()
        self._schedule_render()

    def refresh_all(self) -> None:
        for item in self._items:
            item.clear_pixmap()
        self._schedule_render()

    def current_page(self) -> int:
        return self._current

    def scroll_to_page(self, index: int) -> None:
        if 0 <= index < len(self._items):
            self.verticalScrollBar().setValue(int(self._items[index].pos().y() * self._zoom))
            self._set_current(index)

    # ------------------------------------------------------------ 缩放

    def zoom(self) -> float:
        return self._zoom

    def set_zoom(self, factor: float) -> None:
        factor = max(MIN_ZOOM, min(MAX_ZOOM, factor))
        if abs(factor - self._zoom) < 1e-4:
            return
        self._zoom = factor
        self.setTransform(QTransform().scale(factor, factor))
        self.zoom_changed.emit(factor)
        self._schedule_render()  # 旧位图先拉伸顶着，渲染完成自动替换

    def zoom_by(self, ratio: float) -> None:
        self.set_zoom(self._zoom * ratio)

    def fit_width(self) -> None:
        if not self._items:
            return
        widest = max(item.page_rect().width() for item in self._items)
        available = self.viewport().width() - 28
        if widest > 0 and available > 0:
            self.set_zoom(available / widest)
            self.horizontalScrollBar().setValue(0)
            self._schedule_render()

    # ------------------------------------------------------------ 工具

    def set_tool(self, tool: str) -> None:
        self._tool = tool
        if tool == TOOL_BROWSE:
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
        elif tool in (TOOL_TEXT, TOOL_REPLACE):
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        else:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.viewport().setCursor(Qt.CursorShape.CrossCursor)

    # ------------------------------------------------------------ 渲染

    def _render_scale(self) -> float:
        dpr = self.devicePixelRatioF() or 1.0
        return max(0.25, min(self._zoom * dpr, MAX_RENDER_SCALE))

    def _schedule_render(self) -> None:
        self._render_timer.start()

    def _render_visible(self) -> None:
        if self._doc is None:
            return
        scale = self._render_scale()
        visible = self.mapToScene(self.viewport().rect()).boundingRect()
        for item in self._items:
            if not item.sceneBoundingRect().intersects(visible):
                continue
            if item.needs_render(scale):
                self._render_item(item, scale)

    def _render_item(self, item: _PageItem, scale: float) -> None:
        if item.index >= self._doc.page_count:
            return
        page = self._doc[item.index]
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        image = QImage(
            pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888
        ).copy()  # QImage 不持有 samples 内存，必须拷贝
        item.set_pixmap(QPixmap.fromImage(image), scale)

    # ------------------------------------------------------------ 事件

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._schedule_render()

    def wheelEvent(self, event) -> None:  # noqa: N802
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_by(1.25 if event.angleDelta().y() > 0 else 0.8)
            event.accept()
            return
        super().wheelEvent(event)

    def _item_at(self, view_pos: QPoint) -> _PageItem | None:
        for item in self._scene.items(self.mapToScene(view_pos)):
            if isinstance(item, _PageItem):
                return item
        return None

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._tool in (TOOL_TEXT, TOOL_REPLACE, TOOL_REDACT):
            item = self._item_at(event.position().toPoint())
            if item is not None:
                self._press_item = item
                self._press_scene = self.mapToScene(event.position().toPoint())
                self._set_current(item.index)
                if self._tool in (TOOL_REDACT, TOOL_REPLACE):
                    pen = QPen(QColor("#ef4444"))
                    pen.setWidth(0)
                    pen.setStyle(Qt.PenStyle.DashLine)
                    self._rubber = QGraphicsRectItem()
                    self._rubber.setPen(pen)
                    self._rubber.setBrush(QColor(239, 68, 68, 50))
                    self._scene.addItem(self._rubber)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._rubber is not None and self._press_scene is not None:
            current = self.mapToScene(event.position().toPoint())
            self._rubber.setRect(QRectF(self._press_scene, current).normalized())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._press_item is not None:
            item = self._press_item
            press_scene = self._press_scene or self.mapToScene(event.position().toPoint())
            self._press_item = None
            self._press_scene = None
            start = item.mapFromScene(press_scene)
            end = item.mapFromScene(self.mapToScene(event.position().toPoint()))
            if self._rubber is not None:
                self._scene.removeItem(self._rubber)
                self._rubber = None
                rect = QRectF(start, end).normalized().intersected(item.page_rect())
                if rect.width() >= 2 and rect.height() >= 2:
                    self.region_selected.emit(
                        item.index, rect.left(), rect.top(), rect.right(), rect.bottom()
                    )
            elif self._tool in (TOOL_TEXT, TOOL_REPLACE):
                if (start - end).manhattanLength() <= 3:  # 仅单击（非拖拽）才触发
                    self.text_clicked.emit(item.index, start.x(), start.y())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # ------------------------------------------------------------ 当前页

    def _set_current(self, index: int) -> None:
        if index != self._current:
            self._current = index
            self.current_page_changed.emit(index)

    def _sync_current_page(self) -> None:
        if not self._items:
            return
        top = self.mapToScene(QPoint(0, 0)).y() + 1
        index = 0
        for item in self._items:
            if item.pos().y() <= top:
                index = item.index
            else:
                break
        self._set_current(index)


# ---------------------------------------------------------------- 页面
class PdfEditorPage(QWidget):
    """PDF 编辑页：打开 → 编辑 → 另存为新的 PDF。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._doc = None
        self._path = ""
        self._dirty = False
        self._undo_stack: list[bytes] = []
        self._tool = TOOL_BROWSE
        self._edit_widgets: list[QWidget] = []

        self._toast = Toast(self)
        self.setAcceptDrops(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)

        # 画布先于工具栏创建：工具栏的缩放按钮要引用画布接口
        self._canvas = _PdfCanvas()
        self._canvas.text_clicked.connect(self._on_canvas_click)
        self._canvas.region_selected.connect(self._on_region_selected)
        self._canvas.current_page_changed.connect(lambda _i: self._update_page_label())
        self._canvas.zoom_changed.connect(lambda z: self._zoom_label.setText(f"{int(z * 100)}%"))
        root.addWidget(self._build_toolbar())

        body = QHBoxLayout()
        body.setSpacing(16)
        body.addWidget(self._canvas, 1)
        body.addWidget(self._build_panel())
        root.addLayout(body, 1)

        self._select_tool(TOOL_BROWSE)
        self._sync_actions()
        self._update_page_label()

    # ------------------------------------------------------------ 顶部工具条

    def _build_toolbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("Card")
        line = QHBoxLayout(bar)
        line.setContentsMargins(16, 12, 16, 12)
        line.setSpacing(8)

        open_btn = QPushButton("📂 打开 PDF")
        open_btn.setProperty("cssClass", "btn-primary")
        open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_btn.clicked.connect(self._open_dialog)
        line.addWidget(open_btn)

        self._file_label = QLabel("未打开文件（可将 PDF 拖到本页面）")
        self._file_label.setObjectName("MutedText")
        line.addWidget(self._file_label)
        line.addStretch(1)

        zoom_out = self._small_button("－", lambda: self._canvas.zoom_by(0.8))
        self._zoom_label = QLabel("100%")
        self._zoom_label.setObjectName("MutedText")
        self._zoom_label.setFixedWidth(52)
        self._zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        zoom_in = self._small_button("＋", lambda: self._canvas.zoom_by(1.25))
        fit = self._small_button("适应宽度", self._canvas.fit_width)
        for w in (zoom_out, self._zoom_label, zoom_in, fit):
            line.addWidget(w)

        self._btn_undo = self._small_button("↶ 撤销", self._undo)
        line.addWidget(self._btn_undo)

        self._dirty_label = QLabel("")
        self._dirty_label.setObjectName("MutedText")
        line.addWidget(self._dirty_label)

        self._btn_save = QPushButton("💾 另存为新的 PDF")
        self._btn_save.setProperty("cssClass", "btn-success")
        self._btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_save.clicked.connect(self._save_as)
        line.addWidget(self._btn_save)

        self._edit_widgets.extend([zoom_out, zoom_in, fit, self._btn_undo, self._btn_save])
        return bar

    def _small_button(self, text: str, handler) -> QPushButton:
        btn = QPushButton(text)
        btn.setProperty("cssClass", "btn-default")
        btn.setFixedHeight(30)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(handler)
        return btn

    # ------------------------------------------------------------ 右侧面板

    def _panel_card(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)
        label = QLabel(title)
        label.setObjectName("CardTitle")
        layout.addWidget(label)
        return card, layout

    def _build_panel(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(276)
        root = QVBoxLayout(panel)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        # ---- 工具选择
        card, layout = self._panel_card("编辑工具")
        layout.setSpacing(8)
        grid = QGridLayout()
        grid.setSpacing(8)
        grid.setColumnStretch(0, 1)  # 两列等宽：按钮不因文字长短错落
        grid.setColumnStretch(1, 1)
        self._tool_buttons: dict[str, QPushButton] = {}
        tools = (
            (TOOL_BROWSE, "浏览/平移"),
            (TOOL_TEXT, "添加文字"),
            (TOOL_REPLACE, "修改文字"),
            (TOOL_REDACT, "涂黑遮盖"),
        )
        for i, (key, text) in enumerate(tools):
            btn = QPushButton(text)
            btn.setCheckable(True)
            btn.setProperty("cssClass", "btn-auto-fit")
            btn.setFixedHeight(32)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, k=key: self._select_tool(k))
            grid.addWidget(btn, i // 2, i % 2)
            self._tool_buttons[key] = btn
        layout.addLayout(grid)
        layout.addSpacing(4)  # 与下方用法说明拉开距离
        # 用法逐条独立成行：行距由布局间距控制，比多行文本更透气
        for text in (
            "浏览：按住左键拖拽平移",
            "添加文字：点击页面放置新文字",
            "修改文字：点击原文改写，或框选局部",
            "涂黑遮盖：框选区域（区域文字被移除）",
        ):
            hint = QLabel(text)
            hint.setObjectName("MutedText")
            hint.setWordWrap(True)
            layout.addWidget(hint)
        # 涂黑填充色跟随涂黑工具，避免再多占一张卡片的高度
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(QLabel("涂黑颜色"))
        self._redact_fill = QComboBox()
        for name, rgb in REDACT_FILLS:
            self._redact_fill.addItem(name, rgb)
        row.addWidget(self._redact_fill, 1)
        layout.addLayout(row)
        root.addWidget(card)

        # ---- 添加文字
        card, layout = self._panel_card("添加文字")
        self._text_input = QLineEdit()
        self._text_input.setPlaceholderText("要添加的文字（支持中文）")
        layout.addWidget(self._text_input)
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(QLabel("字号"))
        self._text_size = QSpinBox()
        self._text_size.setRange(6, 200)
        self._text_size.setValue(14)
        row.addWidget(self._text_size, 1)
        row.addWidget(QLabel("颜色"))
        self._text_color = QComboBox()
        for name, rgb in TEXT_COLORS:
            self._text_color.addItem(name, rgb)
        row.addWidget(self._text_color, 1)
        layout.addLayout(row)
        root.addWidget(card)

        # ---- 水印
        card, layout = self._panel_card("添加水印")
        self._wm_input = QLineEdit()
        self._wm_input.setPlaceholderText("水印文字，如：内部资料")
        layout.addWidget(self._wm_input)
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(QLabel("字号"))
        self._wm_size = QSpinBox()
        self._wm_size.setRange(12, 300)
        self._wm_size.setValue(40)
        row.addWidget(self._wm_size, 1)
        layout.addLayout(row)
        row = QHBoxLayout()
        row.setSpacing(6)
        for text, all_pages in (("当前页", False), ("全部页", True)):
            btn = QPushButton(text)
            btn.setProperty("cssClass", "btn-default")
            btn.setFixedHeight(30)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, a=all_pages: self._add_watermark(a))
            row.addWidget(btn)
        layout.addLayout(row)
        root.addWidget(card)

        # ---- 页面操作
        card, layout = self._panel_card("页面操作")
        self._page_label = QLabel("当前页：- / -")
        self._page_label.setObjectName("MutedText")
        layout.addWidget(self._page_label)
        row = QHBoxLayout()
        row.setSpacing(6)
        for text, handler in (("↺ 左转 90°", lambda: self._rotate(-90)), ("↻ 右转 90°", lambda: self._rotate(90))):
            btn = QPushButton(text)
            btn.setProperty("cssClass", "btn-default")
            btn.setFixedHeight(30)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, h=handler: h())
            row.addWidget(btn)
        layout.addLayout(row)
        row = QHBoxLayout()
        row.setSpacing(6)
        for text, handler in (("插入空白页", self._insert_blank_page), ("删除当前页", self._delete_page)):
            btn = QPushButton(text)
            btn.setProperty("cssClass", "btn-default")
            btn.setFixedHeight(30)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, h=handler: h())
            row.addWidget(btn)
        layout.addLayout(row)
        root.addWidget(card)

        root.addStretch(1)
        self._edit_widgets.extend(
            [*self._tool_buttons.values(), self._text_input, self._text_size, self._text_color,
             self._wm_input, self._wm_size, self._redact_fill]
        )
        # 面板内剩余按钮也纳入统一启停
        for btn in panel.findChildren(QPushButton):
            if btn not in self._edit_widgets:
                self._edit_widgets.append(btn)

        # 工具面板整体较高：包一层滚动区——窗口不够高时滚动查看，
        # 而不是把卡片和按钮压扁重叠（QVBoxLayout 空间不足会挤压子项高度）。
        scroll = QScrollArea()
        scroll.setObjectName("EditorPanel")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFixedWidth(276)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.viewport().setAutoFillBackground(False)
        panel.setAutoFillBackground(False)
        scroll.setWidget(panel)
        return scroll

    # ------------------------------------------------------------ 打开 / 保存

    def _open_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "打开 PDF", "", "PDF 文件 (*.pdf)")
        if path:
            self.open_pdf(path)

    def open_pdf(self, path: str) -> None:
        if self._dirty and not self._confirm_discard():
            return
        try:
            doc = pymupdf.open(path)
        except Exception as exc:  # noqa: BLE001 — 损坏/非 PDF 文件统一提示
            QMessageBox.warning(self, "打开失败", f"无法打开该 PDF：\n{exc}")
            return
        if doc.needs_pass:
            doc.close()
            QMessageBox.warning(self, "不支持加密文档", "该 PDF 需要密码才能打开，暂不支持编辑。")
            return
        if doc.page_count == 0:
            doc.close()
            QMessageBox.warning(self, "打开失败", "该 PDF 没有任何页面。")
            return

        self._close_doc()
        self._doc = doc
        self._path = str(Path(path).resolve())
        self._undo_stack.clear()
        self._dirty = False
        self._canvas.load(doc)
        self._canvas.fit_width()

        name = Path(path).name
        self._file_label.setText(f"{name}（{doc.page_count} 页）")
        self._file_label.setToolTip(self._path)
        self._select_tool(TOOL_BROWSE)
        self._sync_actions()
        self._update_page_label()
        self._toast.show_message(f"已打开：{name}")

    def _save_as(self) -> None:
        if self._doc is None:
            return
        stem = Path(self._path).stem if self._path else "document"
        default = str(Path(self._path).with_name(f"{stem}_edited.pdf")) if self._path else f"{stem}_edited.pdf"
        path, _ = QFileDialog.getSaveFileName(self, "另存为新的 PDF", default, "PDF 文件 (*.pdf)")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        if self._path and Path(path).resolve() == Path(self._path):
            QMessageBox.warning(self, "请另存为新文件", "不能覆盖正在编辑的原文件，请换一个文件名或目录。")
            return
        try:
            self._doc.save(path, garbage=3, deflate=True)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "保存失败", f"无法写入文件：\n{exc}")
            return
        self._dirty = False
        self._sync_actions()
        self._toast.show_message(f"已另存为：{Path(path).name}", msec=4000)

    def _confirm_discard(self) -> bool:
        answer = QMessageBox.question(
            self, "放弃未保存的修改", "当前 PDF 有未保存的修改，打开新文件将丢失这些修改。是否继续？"
        )
        return answer == QMessageBox.StandardButton.Yes

    def _close_doc(self) -> None:
        if self._doc is not None:
            try:
                self._doc.close()
            except Exception:  # noqa: BLE001
                pass
            self._doc = None

    # ------------------------------------------------------------ 编辑操作

    def _push_undo(self) -> None:
        if self._doc is None:
            return
        try:
            self._undo_stack.append(self._doc.tobytes())
        except Exception:  # noqa: BLE001 — 快照失败不阻断编辑
            return
        if len(self._undo_stack) > UNDO_LIMIT:
            self._undo_stack.pop(0)

    def _undo(self) -> None:
        if self._doc is None or not self._undo_stack:
            return
        data = self._undo_stack.pop()
        self._doc.close()
        self._doc = pymupdf.open(stream=data, filetype="pdf")
        self._canvas.load(self._doc)
        self._dirty = True
        self._sync_actions()
        self._toast.show_message("已撤销上一步编辑")

    def _after_edit(self, page_index: int | None) -> None:
        if page_index is None:
            self._canvas.refresh_all()
        else:
            self._canvas.refresh_page(page_index)
        self._dirty = True
        self._sync_actions()
        self._toast.show_message("已编辑，记得「另存为新的 PDF」导出")

    def _on_canvas_click(self, page_index: int, x: float, y: float) -> None:
        """画布单击：按当前工具分派（添加新文字 / 改写已有文字）。"""
        if self._tool == TOOL_REPLACE:
            self._edit_existing_text(page_index, x, y)
        else:
            self._insert_text(page_index, x, y)

    def _on_region_selected(self, page_index: int, x0: float, y0: float,
                            x1: float, y1: float) -> None:
        """画布框选：按当前工具分派（涂黑遮盖 / 只改选区内文字）。"""
        if self._tool == TOOL_REPLACE:
            self._edit_existing_text(page_index, x0, y0, rect=(x0, y0, x1, y1))
        else:
            self._redact_region(page_index, x0, y0, x1, y1)

    def _insert_text(self, page_index: int, x: float, y: float) -> None:
        if self._doc is None:
            return
        text = self._text_input.text().strip()
        if not text:
            self._toast.show_message("请先在右侧「添加文字」中输入内容")
            return
        fontsize = float(self._text_size.value())
        color = self._text_color.currentData()
        page = self._doc[page_index]
        self._push_undo()
        # 点击点作为文字左上角：视觉上向下平移一个行高，再换算到内容坐标
        point = _visual_point(page, x, y + fontsize)
        _insert_text(page, point, text, fontsize=fontsize, color=color, rotate=_visual_angle(page, 0))
        self._after_edit(page_index)

    def _edit_existing_text(self, page_index: int, x: float, y: float, rect=None) -> None:
        """改写页面上的原有文字：命中原文 → 弹窗修改 → 删旧写新。

        PDF 里没有"可编辑的文本对象"，所以实际动作是：抹除命中文字所占区域，
        再按原有的基线起点、字号、颜色重写。命中范围：单击取该处所在整行，
        拖拽框选则只取选区内与框相交的字符。
        """
        if self._doc is None:
            return
        page = self._doc[page_index]
        if rect is not None:
            hit = _find_text_hit(page, rect=_visual_rect(page, *rect))
            empty_hint = "选区内没有可编辑的文字（图片/扫描件中的文字无法编辑）"
            prompt = f"第 {page_index + 1} 页 · 选区内文字如下，修改后点「确定」替换："
        else:
            hit = _find_text_hit(page, point=_visual_point(page, x, y))
            empty_hint = "此处没有可编辑的文字（图片/扫描件中的文字无法编辑）"
            prompt = f"第 {page_index + 1} 页 · 选中文字如下，修改后点「确定」替换："
        if hit is None:
            self._toast.show_message(empty_hint)
            return
        old_text, hit_rect, origin, size, color, _font = hit
        new_text, ok = QInputDialog.getMultiLineText(self, "修改文字", prompt, old_text)
        if not ok:
            return
        new_text = new_text.strip()
        if not new_text or new_text == old_text:
            return
        self._push_undo()
        page.add_redact_annot(hit_rect, fill=(1, 1, 1))  # 先抹除原文字
        page.apply_redactions()
        # 按原字号/原位置重写；中英文分别用内置字体，字形可能与原文略有差异
        _insert_text(
            page,
            origin,
            new_text,
            fontsize=size,
            color=color,
            rotate=_visual_angle(page, 0),
            fontname=CJK_FONT if any(ord(ch) > 0x7F for ch in new_text) else "helv",
        )
        self._after_edit(page_index)
        self._toast.show_message(f"已替换：{_short(old_text)} → {_short(new_text)}", msec=4000)

    def _add_watermark(self, all_pages: bool) -> None:
        if self._doc is None:
            return
        text = self._wm_input.text().strip()
        if not text:
            self._toast.show_message("请先在右侧「添加水印」中输入内容")
            return
        size = float(self._wm_size.value())
        targets = range(self._doc.page_count) if all_pages else [self._canvas.current_page()]
        self._push_undo()
        for index in targets:
            page = self._doc[index]
            width, height = page.rect.width, page.rect.height
            # 估算文字宽度（中日韩字符按 1 个字宽，其余按 0.56）用于居中
            estimate = sum(size if ord(ch) > 0x2E80 else size * 0.56 for ch in text)
            start = _visual_point(page, width / 2 - estimate / 2, height / 2 + size / 3)
            morph = (
                _visual_point(page, width / 2, height / 2),
                pymupdf.Matrix(_visual_angle(page, 45)),
            )
            _insert_text(
                page, start, text, fontsize=size, color=(0.55, 0.55, 0.55),
                morph=morph, opacity=0.45,
            )
        self._after_edit(None)

    def _redact_region(self, page_index: int, x0: float, y0: float, x1: float, y1: float) -> None:
        if self._doc is None:
            return
        page = self._doc[page_index]
        fill = self._redact_fill.currentData()
        self._push_undo()
        page.add_redact_annot(_visual_rect(page, x0, y0, x1, y1), fill=fill)
        page.apply_redactions()
        self._after_edit(page_index)

    def _rotate(self, delta: int) -> None:
        if self._doc is None:
            return
        index = self._canvas.current_page()
        page = self._doc[index]
        self._push_undo()
        page.set_rotation((page.rotation + delta) % 360)
        self._canvas.rebuild()
        self._canvas.scroll_to_page(index)
        self._after_edit(None)
        self._update_page_label()

    def _insert_blank_page(self) -> None:
        if self._doc is None:
            return
        index = self._canvas.current_page()
        page = self._doc[index]
        width, height = page.rect.width, page.rect.height
        self._push_undo()
        self._doc.new_page(pno=index + 1, width=width, height=height)
        self._canvas.rebuild()
        self._canvas.scroll_to_page(index + 1)
        self._after_edit(None)
        self._update_page_label()
        self._toast.show_message("已在当前页后插入空白页")

    def _delete_page(self) -> None:
        if self._doc is None:
            return
        if self._doc.page_count <= 1:
            self._toast.show_message("至少需要保留一页，无法删除")
            return
        index = self._canvas.current_page()
        answer = QMessageBox.question(self, "确认删除", f"确认删除第 {index + 1} 页？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._push_undo()
        self._doc.delete_page(index)
        target = min(index, self._doc.page_count - 1)
        self._canvas.rebuild()
        self._canvas.scroll_to_page(target)
        self._after_edit(None)
        self._update_page_label()
        self._toast.show_message(f"已删除第 {index + 1} 页")

    # ------------------------------------------------------------ 状态

    def _select_tool(self, key: str) -> None:
        self._tool = key
        for name, btn in self._tool_buttons.items():
            btn.setChecked(name == key)
        self._canvas.set_tool(key)

    def _update_page_label(self) -> None:
        if self._doc is None:
            self._page_label.setText("当前页：- / -")
        else:
            self._page_label.setText(f"当前页：{self._canvas.current_page() + 1} / {self._doc.page_count}")

    def _sync_actions(self) -> None:
        has_doc = self._doc is not None
        for widget in self._edit_widgets:
            widget.setEnabled(has_doc)
        self._btn_undo.setEnabled(has_doc and bool(self._undo_stack))
        self._dirty_label.setText("● 有未保存的修改" if self._dirty else "")
        self._dirty_label.setStyleSheet(
            f"color: {token('AMBER_TEXT')}; font-weight: 600;" if self._dirty else ""
        )

    # ------------------------------------------------------------ 拖拽

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        if any(url.toLocalFile().lower().endswith(".pdf") for url in urls):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith(".pdf"):
                self.open_pdf(path)
                break
