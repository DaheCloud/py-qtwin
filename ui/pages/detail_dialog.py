"""文档详情弹窗（对应 ui.html 的 #detail-modal）。

左侧 PDF 预览（可缩放：工具条 / Ctrl+滚轮），右侧识别字段列表 + 交叉验证结果。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from pdf.states import final_display_status
from ui.field_labels import field_label
from ui.styles import GREEN, RED, token, ui_font
from ui.widgets.common import Badge

# 证据维度中文名（方案 §20：让复核人员直接看到"为什么需要复核"）
_EVIDENCE_LABELS = {
    "anchor": "锚点定位",
    "geometry": "取值位置",
    "region": "页面区域",
    "pattern": "格式",
    "cross_engine": "双引擎一致",
    "business": "业务关系",
}
# 证据分低于此值视为"弱证据"（与 evidence/scorer.FIELD_MEDIUM 保持一致）
_EVIDENCE_WEAK = 0.75


def _evidence_lines(evidence_json: str | None, confidence: float | None) -> list[str]:
    """把落库的字段证据转成几行可读文本（可信度 + 逐维度 + 原因）。"""
    import json as _json

    lines: list[str] = []
    data = None
    if evidence_json:
        try:
            data = _json.loads(evidence_json)
        except (TypeError, ValueError):
            data = None
    score = None
    if isinstance(data, dict):
        score = data.get("score")
    if score is None:
        score = confidence
    if score is not None:
        lines.append(f"可信度 {float(score) * 100:.0f}%")
    if isinstance(data, dict):
        for name, value in (data.get("evidence") or {}).items():
            mark = "✓" if float(value) >= _EVIDENCE_WEAK else "✗"
            lines.append(
                f"{mark} {_EVIDENCE_LABELS.get(name, name)} {float(value) * 100:.0f}%"
            )
        for reason in (data.get("reasons") or [])[:2]:
            lines.append(f"· {reason}")
    return lines


def _clear_layout(layout) -> None:
    """递归清空布局中的全部控件与子布局（确认操作后重建用）。"""
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
        child = item.layout()
        if child is not None:
            _clear_layout(child)


def _badge_for_status(status: str) -> tuple[str, str]:
    return {
        "success": ("success", "准确"),
        "manual_review": ("warning", "可疑/待校验"),
        "warning": ("warning", "可疑/待校验"),
        "failed": ("failed", "解析失败"),
        "needs_ocr": ("info", "等待 OCR"),
        "processing": ("info", "解析中"),
        "pending": ("info", "等待解析"),
    }.get(status, ("info", status))


class _PdfRenderSignals(QObject):
    finished = Signal(int, int, int, QImage, str)


class _PdfRenderTask(QRunnable):
    """在线程池中栅格化一页，复杂矢量 PDF 不占用 GUI 线程。"""

    def __init__(self, request_id: int, path: str, page_index: int, signals: _PdfRenderSignals) -> None:
        super().__init__()
        self.request_id = request_id
        self.path = path
        self.page_index = page_index
        self.signals = signals

    def run(self) -> None:
        try:
            import pymupdf

            with pymupdf.open(self.path) as document:
                page_count = document.page_count
                if page_count < 1:
                    raise ValueError("PDF 没有可显示的页面")
                page_index = min(max(0, self.page_index), page_count - 1)
                page = document.load_page(page_index)
                # 固定像素上限避免超宽表格按原始矢量尺寸生成巨型图像。
                width = max(1.0, page.rect.width)
                height = max(1.0, page.rect.height)
                scale = min(
                    4.0,
                    2400 / width,
                    (20_000_000 / (width * height)) ** 0.5,
                )
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                image = QImage(
                    pixmap.samples,
                    pixmap.width,
                    pixmap.height,
                    pixmap.stride,
                    QImage.Format.Format_RGB888,
                ).copy()
            self.signals.finished.emit(
                self.request_id, page_index, page_count, image, ""
            )
        except Exception as exc:  # PDF 损坏等错误需要回传到预览区，而不是终止线程
            try:
                self.signals.finished.emit(
                    self.request_id, self.page_index, 0, QImage(), str(exc)
                )
            except RuntimeError:
                pass  # 弹窗已关闭，接收信号的 QObject 已销毁


class _ZoomablePdfView(QScrollArea):
    """显示后台栅格化页面，支持缩放和抓手拖动。"""

    MIN_ZOOM, MAX_ZOOM = 0.3, 4.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.on_zoom = None  # callable(factor: float)，由宿主设置
        self._pan_origin: QPoint | None = None
        self._image = QImage()
        self._zoom_factor = 1.0
        self._fit_width = True
        self._page = QLabel()
        self._page.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._page.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setWidget(self._page)
        self.setWidgetResizable(False)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 手掌光标提示"按住可拖动"
        self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)

    def wheelEvent(self, event) -> None:  # noqa: N802
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            step = 1.25 if event.angleDelta().y() > 0 else 0.8
            self.apply_zoom(self.zoomFactor() * step)
            event.accept()
            return
        super().wheelEvent(event)

    # ---- 抓手拖拽平移（放大后内容超出视口时按住左键拖动）

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._pan_origin = event.position().toPoint()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._pan_origin is not None:
            delta = self._pan_origin - event.position().toPoint()
            h_bar = self.horizontalScrollBar()
            v_bar = self.verticalScrollBar()
            h_bar.setValue(h_bar.value() + delta.x())
            v_bar.setValue(v_bar.value() + delta.y())
            self._pan_origin = event.position().toPoint()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._pan_origin is not None:
            self._pan_origin = None
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def apply_zoom(self, factor: float) -> None:
        factor = max(self.MIN_ZOOM, min(self.MAX_ZOOM, factor))
        self._fit_width = False
        self._zoom_factor = factor
        self._refresh_pixmap()
        if self.on_zoom is not None:
            self.on_zoom(factor)

    def zoomFactor(self) -> float:  # noqa: N802 - 与原 QPdfView API 保持一致
        return self._zoom_factor

    def fit_width(self) -> None:
        self._fit_width = True
        self._zoom_factor = 1.0
        self._refresh_pixmap()

    def set_image(self, image: QImage) -> None:
        self._image = image
        self._refresh_pixmap()

    def set_message(self, text: str) -> None:
        self._image = QImage()
        self._page.setPixmap(QPixmap())
        self._page.setText(text)
        self._page.adjustSize()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._fit_width:
            self._refresh_pixmap()

    def _refresh_pixmap(self) -> None:
        if self._image.isNull():
            return
        available_width = max(1, self.viewport().width() - 16)
        fit_scale = min(1.0, available_width / self._image.width())
        scale = fit_scale * (1.0 if self._fit_width else self._zoom_factor)
        target_width = max(1, round(self._image.width() * scale))
        pixmap = QPixmap.fromImage(self._image).scaledToWidth(
            target_width, Qt.TransformationMode.SmoothTransformation
        )
        self._page.setText("")
        self._page.setPixmap(pixmap)
        self._page.resize(pixmap.size())


class DetailDialog(QDialog):
    """文档详情：左 PDF 预览 + 右字段校验，对应 .modal-body grid。"""

    document_confirmed = Signal(int)  # 人工确认完成后发出（document_id），宿主刷新列表

    def __init__(self, db_path: str, doc_id: int, file_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"文档详情 - {file_name}")
        self.setModal(True)
        # 标题栏带最大化/最小化按钮，全屏查看大尺寸 PDF 更方便
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint
        )
        self.resize(1200, 680)
        self.setObjectName("ModalContainer")
        # 确保弹窗背景使用 QSS 主题色（否则系统深色模式下会回退到深色调色板，
        # 与浅色主题的文字色叠加导致"深底深字"看不清）
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self._doc_id = doc_id
        self._db_path = db_path

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        body = QHBoxLayout()
        body.setSpacing(16)
        root.addLayout(body, 1)

        # ---- 左：PDF 预览 + 缩放工具条
        pdf_wrap = QWidget()
        # 透明 QWidget 在系统深色模式下会透出深色调色板，显式绑定主题背景
        pdf_wrap.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        pdf_wrap.setStyleSheet(f"background: {token('BG_CARD')};")
        pdf_l = QVBoxLayout(pdf_wrap)
        pdf_l.setContentsMargins(0, 0, 0, 0)
        pdf_l.setSpacing(8)

        zoom_bar = QHBoxLayout()
        zoom_bar.setSpacing(6)
        # 注意：不固定宽度——QSS 按钮左右 padding 共 28px，固定太窄会把文字完全挤出
        self._btn_zoom_out = self._tool_button("缩小", lambda: self._pdf_view.apply_zoom(self._pdf_view.zoomFactor() * 0.8))
        self._zoom_label = QLabel("适应宽度")
        self._zoom_label.setObjectName("MutedText")
        self._zoom_label.setFixedWidth(56)
        self._zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._btn_zoom_in = self._tool_button("放大", lambda: self._pdf_view.apply_zoom(self._pdf_view.zoomFactor() * 1.25))
        self._btn_fit = self._tool_button("适应宽度", self._fit_pdf_width)
        for w in (self._btn_zoom_out, self._zoom_label, self._btn_zoom_in, self._btn_fit):
            zoom_bar.addWidget(w)
        # 翻页：单页按需渲染，避免多页 PDF 打开时同步布局全部页面阻塞界面。
        self._btn_page_prev = self._tool_button(
            "上一页", lambda: self._change_page(-1)
        )
        self._btn_page_prev.setEnabled(False)
        self._page_label = QLabel("—/—")
        self._page_label.setObjectName("MutedText")
        self._page_label.setFixedWidth(52)
        self._page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._btn_page_next = self._tool_button(
            "下一页", lambda: self._change_page(1)
        )
        self._btn_page_next.setEnabled(False)
        for w in (self._btn_page_prev, self._page_label, self._btn_page_next):
            zoom_bar.addWidget(w)
        zoom_bar.addStretch(1)
        pdf_l.addLayout(zoom_bar)

        self._pdf_view = _ZoomablePdfView()
        self._pdf_view.setObjectName("PdfPreview")
        self._pdf_view.on_zoom = lambda f: self._zoom_label.setText(f"{int(f * 100)}%")
        self._pdf_path = ""
        self._page_index = 0
        self._page_count = 0
        self._render_request_id = 0
        self._render_signals = _PdfRenderSignals(self)
        self._render_signals.finished.connect(self._on_pdf_rendered)
        pdf_l.addWidget(self._pdf_view, 1)
        body.addWidget(pdf_wrap, 6)  # 预览 : 字段 = 6 : 4

        # ---- 右：字段校验
        right = QWidget()
        right.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        right.setStyleSheet(f"background: {token('BG_CARD')};")
        right_l = QVBoxLayout(right)
        right_l.setContentsMargins(0, 0, 0, 0)
        right_l.setSpacing(8)

        fields_title = QLabel("识别字段校验")
        fields_title.setFont(ui_font(11, 600))
        right_l.addWidget(fields_title)

        status_row = QHBoxLayout()
        status_label = QLabel("识别状态：")
        status_label.setFont(ui_font(10))
        status_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        status_row.addWidget(status_label)
        self._status_badge_host = QVBoxLayout()
        self._status_badge_host.setSpacing(4)
        status_row.addLayout(self._status_badge_host, 1)
        right_l.addLayout(status_row)

        self._fields_area = QScrollArea()
        self._fields_area.setWidgetResizable(True)
        self._fields_area.setFrameShape(QFrame.Shape.NoFrame)
        # 内容随宽度换行铺满，不出现水平滚动条
        self._fields_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._fields_host = QWidget()
        # 滚动区视口无 QSS 时会用系统调色板（深色模式下是深灰），
        # 显式设为卡片背景色，交叉验证文字才清晰
        self._fields_host.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._fields_host.setStyleSheet(f"background: {token('BG_CARD')};")
        self._fields_l = QVBoxLayout(self._fields_host)
        self._fields_l.setContentsMargins(0, 0, 8, 0)
        self._fields_l.setSpacing(6)
        self._fields_area.setWidget(self._fields_host)
        right_l.addWidget(self._fields_area, 1)
        body.addWidget(right, 4)  # 预览 : 字段 = 6 : 4

        # ---- 底部：整体人工确认（仅待人工确认状态显示）
        footer = QHBoxLayout()
        footer.addStretch(1)
        self._confirm_doc_btn = QPushButton("✓ 确认无误，标记为准确")
        self._confirm_doc_btn.setProperty("cssClass", "btn-success")
        self._confirm_doc_btn.setFixedHeight(32)
        self._confirm_doc_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._confirm_doc_btn.clicked.connect(self._confirm_document)
        self._confirm_doc_btn.setVisible(False)
        footer.addWidget(self._confirm_doc_btn)
        root.addLayout(footer)

        self.load_document()

    # ------------------------------------------------------------------

    def _tool_button(self, text: str, handler) -> QPushButton:
        btn = QPushButton(text)
        btn.setProperty("cssClass", "btn-default")
        btn.setFixedHeight(28)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(handler)
        return btn

    def _fit_pdf_width(self) -> None:
        """恢复适应宽度模式，倍率显示还原。"""
        self._pdf_view.fit_width()
        self._zoom_label.setText("适应宽度")

    def _update_page_label(self, index: int) -> None:
        """页码标签（当前页/总页数）：连续滚动或点翻页时刷新。"""
        total = self._page_count or 0
        self._page_label.setText(f"{index + 1}/{total}" if total else "—/—")

    def _change_page(self, step: int) -> None:
        target = min(max(0, self._page_index + step), max(0, self._page_count - 1))
        if target != self._page_index:
            self._request_pdf_page(target)

    def _load_pdf(self, path: str) -> None:
        if path != self._pdf_path:
            self._pdf_path = path
            self._page_index = 0
            self._page_count = 0
        self._request_pdf_page(self._page_index)

    def _request_pdf_page(self, page_index: int) -> None:
        self._render_request_id += 1
        self._pdf_view.set_message("正在加载 PDF…")
        task = _PdfRenderTask(
            self._render_request_id, self._pdf_path, page_index, self._render_signals
        )
        QThreadPool.globalInstance().start(task)

    def _on_pdf_rendered(
        self, request_id: int, page_index: int, page_count: int, image: QImage, error: str
    ) -> None:
        if request_id != self._render_request_id:
            return
        if error:
            self._page_count = 0
            self._update_page_label(0)
            self._pdf_view.set_message(f"PDF 加载失败：{error}")
            return
        self._page_index = page_index
        self._page_count = page_count
        self._pdf_view.set_image(image)
        self._update_page_label(page_index)
        self._btn_page_prev.setEnabled(page_index > 0)
        self._btn_page_next.setEnabled(page_index + 1 < page_count)

    def load_document(self) -> None:
        """从 DB 拉取字段与验证结果，并加载 PDF 预览（确认操作后重建）。"""
        from database.db import get_engine, make_session_factory
        from models.document import Document

        engine = get_engine(self._db_path)
        factory = make_session_factory(engine)
        with factory() as session:
            doc = session.get(Document, self._doc_id)
            if doc is None:
                self._add_hint("未找到该文档记录")
                return
            status = doc.status
            processing_status = doc.processing_status
            review_status = doc.review_status
            quality_status = doc.quality_status
            document_score = doc.document_score
            display_status = final_display_status(doc)
            error_reason = doc.error_reason
            identify_confidence = doc.identify_confidence
            parse_confidence = doc.parse_confidence
            fields = [
                (
                    f.field_name,
                    f.raw_value,
                    f.normalized_value,
                    f.parser,
                    float(f.confidence) if f.confidence is not None else None,
                    f.evidence_json,
                    f.strategy,
                    bool(f.fallback_used),
                )
                for f in doc.fields
            ]
            verifications = [
                (v.field_name, v.primary_value, v.secondary_value, v.matched, v.review_status)
                for v in doc.verifications
            ]
            # 明细行（Table Engine 重建结果，方案 §15）：按行展示，便于对照 PDF 核对
            items = [
                (
                    it.row_index,
                    it.name,
                    it.spec,
                    it.unit,
                    it.quantity,
                    it.unit_price,
                    it.amount,
                    it.tax_rate,
                    it.tax,
                )
                for it in doc.items
            ]
            pdf_path = doc.file_path

        # 重建前清空动态区（确认单字段后整体刷新）
        _clear_layout(self._status_badge_host)
        _clear_layout(self._fields_l)

        self._doc_status = status
        # 数据异常型失败允许逐项核对后单条人工放行；PDF 处理错误没有可核对的
        # 完整解析结果，不能确认。失败记录始终不进入列表页的批量确认。
        can_confirm = (
            review_status == "pending"
            and processing_status == "completed"
        ) or (
            not review_status and status in ("manual_review", "warning")
        )
        self._confirm_doc_btn.setVisible(can_confirm)
        self._confirm_doc_btn.setText(
            "✓ 人工确认并标记为准确"
            if status == "failed"
            else "✓ 确认无误，标记为准确"
        )

        kind, text = _badge_for_status(status)
        if status == "success" and review_status == "confirmed":
            text = "人工准确"
        elif status == "success" and review_status == "corrected":
            text = "人工修正"
        badge = Badge(text, kind)
        badge.setFont(ui_font(9, 500))
        self._status_badge_host.addWidget(badge)

        # V2（方案 §9）：三维状态与文档质量分——机器结论不被人工确认覆盖
        if quality_status or document_score is not None or display_status != text:
            detail_parts = [f"展示状态：{display_status}"]
            if quality_status:
                quality_label = {
                    "valid": "质量合格",
                    "warning": "质量存疑",
                    "invalid": "数据异常",
                    "unknown": "质量未知",
                }.get(quality_status, quality_status)
                detail_parts.append(f"质量：{quality_label}")
            if review_status:
                review_label = {
                    "not_required": "无需复核",
                    "pending": "待复核",
                    "confirmed": "已人工确认",
                    "corrected": "已人工修正",
                    "rejected": "已驳回",
                }.get(review_status, review_status)
                detail_parts.append(f"复核：{review_label}")
            if document_score is not None:
                detail_parts.append(f"文档质量分 {float(document_score):.2f}")
            dimension = QLabel(" · ".join(detail_parts))
            dimension.setWordWrap(True)
            dimension.setStyleSheet(f"color: {token('TEXT_MUTED')}; font-size: 12px;")
            self._status_badge_host.addWidget(dimension)

        # 置信度（方案 §10/§31）：识别/解析/综合分开显示，便于判断"是选错模板还是取错值"
        if identify_confidence is not None or parse_confidence is not None:
            overall_confidence = doc.overall_confidence
            confidence = QLabel(
                "识别置信度 "
                f"{identify_confidence if identify_confidence is not None else '—'}"
                " · 解析置信度 "
                f"{parse_confidence if parse_confidence is not None else '—'}"
                + (
                    f" · 综合置信度 {overall_confidence}"
                    if overall_confidence is not None
                    else ""
                )
            )
            confidence.setStyleSheet(
                f"color: {token('TEXT_MUTED')}; font-size: 12px;"
            )
            confidence.setWordWrap(True)
            self._status_badge_host.addWidget(confidence)

        if error_reason:
            reason = QLabel(error_reason)
            reason.setWordWrap(True)
            reason.setStyleSheet(
                f"background: {token('DANGER_TIP_BG')};"
                f"border: 1px solid {token('DANGER_TIP_BORDER')}; border-radius: 6px;"
                f"color: {token('RED_TEXT')}; font-size: 12px; padding: 8px;"
            )
            self._fields_l.addWidget(reason)

        if not fields:
            self._add_hint("没有解析字段")
        else:
            # 需要人工确认的字段：交叉验证不一致（未确认）或被点名在错误原因里
            review_fields = {
                v_name
                for v_name, _, _, v_matched, v_review in verifications
                if not v_matched and v_review != "confirmed"
            }
            if error_reason:
                review_fields.update(seg.split("：", 1)[0].strip() for seg in error_reason.split("；") if seg)
            for row_data in fields:
                name = row_data[0]
                self._add_field_row(*row_data, needs_review=name in review_fields)

        if items:
            self._add_items_section(items)

        if verifications:
            sep = QFrame()
            sep.setFixedHeight(1)
            sep.setObjectName("Divider")
            sep.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            self._fields_l.addWidget(sep)
            v_title = QLabel("交叉验证（pdfplumber）")
            v_title.setFont(ui_font(10, 600))
            v_title.setStyleSheet(f"color: {token('TITLE_TEXT')};")
            self._fields_l.addWidget(v_title)
            for name, primary, secondary, matched, review in verifications:
                self._add_verification_row(name, primary, secondary, matched, review)

        self._fields_l.addStretch(1)

        if pdf_path and Path(pdf_path).exists():
            self._load_pdf(str(pdf_path))
        else:
            self._pdf_path = ""
            self._page_count = 0
            self._update_page_label(0)
            self._pdf_view.set_message("PDF 文件不存在")
            self._btn_page_prev.setEnabled(False)
            self._btn_page_next.setEnabled(False)

    def _add_hint(self, text: str) -> None:
        label = QLabel(text)
        label.setStyleSheet(f"color: {token('TEXT_FAINT')}; font-size: 13px;")
        self._fields_l.addWidget(label)

    def _add_field_row(
        self,
        name: str,
        raw: str | None,
        normalized: str | None,
        parser: str,
        confidence: float | None = None,
        evidence_json: str | None = None,
        strategy: str = "primary",
        fallback_used: bool = False,
        needs_review: bool = False,
    ) -> None:
        row = QFrame()
        row.setObjectName("Card")
        row.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        if needs_review:
            # 待人工确认：红色警示边框
            row.setStyleSheet(f"QFrame#Card {{ border: 1px solid {token('RED')}; }}")
        row_l = QVBoxLayout(row)
        row_l.setContentsMargins(12, 8, 12, 8)
        row_l.setSpacing(2)

        head = QHBoxLayout()
        name_label = QLabel(field_label(name))
        name_label.setFont(ui_font(10, 600))
        name_label.setStyleSheet(f"color: {token('TITLE_TEXT')};")
        head.addWidget(name_label)
        head.addStretch(1)
        edit_btn = QPushButton("修改")
        edit_btn.setProperty("cssClass", "btn-default")
        edit_btn.setFixedHeight(28)
        edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        current = str(normalized if normalized not in (None, "") else raw or "")
        edit_btn.clicked.connect(lambda _=False, n=name, c=current: self._edit_field(n, c))
        head.addWidget(edit_btn)
        row_l.addLayout(head)

        value_label = QLabel(str(normalized if normalized not in (None, "") else raw or "—"))
        value_label.setFont(ui_font(10))
        value_label.setStyleSheet(f"color: {token('TEXT_PRIMARY')};")
        value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        value_label.setWordWrap(True)
        row_l.addWidget(value_label)

        if raw and normalized and raw != normalized:
            raw_label = QLabel(f"原始值：{raw}")
            raw_label.setObjectName("MutedText")
            raw_label.setWordWrap(True)
            row_l.addWidget(raw_label)

        # V2（方案 §20）：逐维度证据 + 来源策略，回答"为什么需要复核"
        lines = _evidence_lines(evidence_json, confidence)
        if strategy == "table":
            lines.append("· 取值来源：明细表格重建")
        elif strategy == "fallback":
            lines.append("· 取值来源：锚点兜底")
        elif fallback_used:
            lines.append("· 该字段曾触发兜底")
        if lines:
            evidence_label = QLabel("\n".join(lines))
            evidence_label.setObjectName("MutedText")
            evidence_label.setWordWrap(True)
            evidence_label.setStyleSheet(f"color: {token('TEXT_MUTED')}; font-size: 12px;")
            row_l.addWidget(evidence_label)

        self._fields_l.addWidget(row)

    def _add_items_section(self, items: list[tuple]) -> None:
        """明细行展示（表格引擎重建结果）：行关联关系已建立，逐行对照 PDF 核对。"""
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setObjectName("Divider")
        sep.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._fields_l.addWidget(sep)

        title = QLabel(f"商品明细（{len(items)} 行 · 表格引擎重建）")
        title.setFont(ui_font(10, 600))
        title.setStyleSheet(f"color: {token('TITLE_TEXT')};")
        self._fields_l.addWidget(title)

        columns = ("序号", "名称", "规格", "单位", "数量", "单价", "金额", "税率", "税额")
        head = QLabel("　".join(columns))
        head.setObjectName("MutedText")
        head.setWordWrap(True)
        self._fields_l.addWidget(head)

        for row in items:
            text = "　".join("—" if value in (None, "") else str(value) for value in row)
            label = QLabel(text)
            label.setFont(ui_font(10))
            label.setStyleSheet(f"color: {token('TEXT_PRIMARY')};")
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setWordWrap(True)
            self._fields_l.addWidget(label)

    def _add_verification_row(
        self, name: str, primary: str | None, secondary: str | None, matched: bool, review_status: str = "pending"
    ) -> None:
        row = QHBoxLayout()
        row.setContentsMargins(0, 2, 0, 2)
        confirmed = review_status == "confirmed"
        mark = QLabel("✓" if (matched or confirmed) else "✗")
        mark.setStyleSheet(f"color: {GREEN if (matched or confirmed) else RED}; font-weight: 600;")
        row.addWidget(mark)
        suffix = "" if matched else ("（已人工确认）" if confirmed else "")
        text = QLabel(f"{field_label(name)}：{primary or '—'}  ↔  {secondary or '—'}{suffix}")
        text.setFont(ui_font(10))
        text.setStyleSheet(f"color: {token('TEXT_PRIMARY')};")
        text.setWordWrap(True)  # 长值自动换行，避免撑出水平滚动条
        row.addWidget(text, 1)
        if not matched and not confirmed:
            # 双引擎不一致：人工核对 PDF 原文后可确认取值正确
            btn = QPushButton("确认正确")
            btn.setProperty("cssClass", "btn-default")
            btn.setFixedHeight(28)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, n=name, v=primary: self._confirm_field(n, v))
            row.addWidget(btn)
        self._fields_l.addLayout(row)

    # ------------------------------------------------------------ 人工确认

    def _edit_field(self, field_name: str, current: str) -> None:
        """人工修改字段取值：更新字段值，并把该字段的验证记录标记为已确认。"""
        new_value, ok = QInputDialog.getText(
            self, "修改字段值", f"{field_label(field_name)}（对照左侧 PDF 原文填写正确值）：", text=current
        )
        if not ok:
            return
        new_value = new_value.strip()
        if new_value == current:
            return

        from datetime import datetime, timezone

        from sqlalchemy import select

        from database.db import get_engine, make_session_factory
        from models.document import AuditLog, ExtractedField, VerificationResult

        engine = get_engine(self._db_path)
        factory = make_session_factory(engine)
        with factory() as session:
            f = session.scalars(
                select(ExtractedField).where(
                    ExtractedField.document_id == self._doc_id,
                    ExtractedField.field_name == field_name,
                )
            ).one_or_none()
            if f is None:
                return
            old_value = f.normalized_value
            f.normalized_value = new_value
            v = session.scalars(
                select(VerificationResult).where(
                    VerificationResult.document_id == self._doc_id,
                    VerificationResult.field_name == field_name,
                )
            ).one_or_none()
            if v is not None:
                v.review_status = "confirmed"
                v.reviewed_value = new_value
                v.reviewed_at = datetime.now(timezone.utc)
            session.add(
                AuditLog(
                    document_id=self._doc_id,
                    action="edit_field",
                    detail=f"field={field_name} old={old_value!r} new={new_value!r}",
                )
            )
            session.commit()
        self.load_document()

    def _confirm_field(self, field_name: str, value: str | None) -> None:
        """单字段人工确认：pending → confirmed，并记录复核值与时间。"""
        from datetime import datetime, timezone

        from sqlalchemy import select

        from database.db import get_engine, make_session_factory
        from models.document import AuditLog, VerificationResult

        engine = get_engine(self._db_path)
        factory = make_session_factory(engine)
        with factory() as session:
            v = session.scalars(
                select(VerificationResult).where(
                    VerificationResult.document_id == self._doc_id,
                    VerificationResult.field_name == field_name,
                )
            ).one_or_none()
            if v is None:
                return
            v.review_status = "confirmed"
            v.reviewed_value = value
            v.reviewed_at = datetime.now(timezone.utc)
            session.add(
                AuditLog(
                    document_id=self._doc_id,
                    action="confirm_field",
                    detail=f"field={field_name} value={value!r}",
                )
            )
            session.commit()
        self.load_document()  # 重建列表反映确认结果

    def _confirm_document(self) -> None:
        """整体确认：全部待复核项标记确认，文档状态流转为准确。

        落库语义统一由 services/review_service.py 处理，
        保证两条入口结果一致。
        """
        from database.db import get_engine, make_session_factory
        from models.document import Document
        from services.review_service import confirm_document

        message = "确认所有字段与 PDF 原文一致，并将该文档标记为「准确」？"
        if self._doc_status == "failed":
            message = (
                "该记录被机器判定为数据异常。\n"
                "请确认已逐项核对提取结果与 PDF 原文一致，是否人工放行并标记为「准确」？"
            )
        answer = QMessageBox.question(self, "人工确认", message)
        if answer != QMessageBox.StandardButton.Yes:
            return
        engine = get_engine(self._db_path)
        factory = make_session_factory(engine)
        with factory() as session:
            doc = session.get(Document, self._doc_id)
            if doc is None:
                return
            confirm_document(session, doc, source="detail_dialog")
            session.commit()
        self.load_document()
        self.document_confirmed.emit(self._doc_id)
