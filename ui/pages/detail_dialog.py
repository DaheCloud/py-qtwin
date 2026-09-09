"""文档详情弹窗（对应 ui.html 的 #detail-modal）。

左侧 QPdfView 预览原 PDF，右侧识别字段列表 + 交叉验证结果。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QHBoxLayout as _HBox,  # noqa: F401
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ui.styles import ui_font
from ui.widgets.common import Badge


def _badge_for_status(status: str) -> tuple[str, str]:
    return {
        "success": ("success", "准确"),
        "manual_review": ("warning", "可疑/待校验"),
        "warning": ("warning", "可疑/待校验"),
        "failed": ("failed", "解析失败"),
        "processing": ("info", "解析中"),
        "pending": ("info", "等待解析"),
    }.get(status, ("info", status))


class DetailDialog(QDialog):
    """文档详情：左 PDF 预览（1.2fr）+ 右字段校验（1fr），对应 .modal-body grid。"""

    def __init__(self, db_path: str, doc_id: int, file_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"文档详情 - {file_name}")
        self.setModal(True)
        self.resize(980, 640)
        self.setObjectName("ModalContainer")

        self._doc_id = doc_id
        self._db_path = db_path

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- 标题栏
        head = QFrame()
        head.setFixedHeight(52)
        head.setStyleSheet("background: #ffffff; border-bottom: 1px solid #e2e8f0;")
        head_l = QHBoxLayout(head)
        head_l.setContentsMargins(20, 0, 20, 0)
        self._title = QLabel(f"文档详情 - {file_name}")
        self._title.setFont(ui_font(11, 600))
        head_l.addWidget(self._title)
        head_l.addStretch(1)
        close_btn = QPushButton("✕")
        close_btn.setProperty("cssClass", "btn-default")
        close_btn.setFixedWidth(36)
        close_btn.clicked.connect(self.reject)
        head_l.addWidget(close_btn)
        root.addWidget(head)

        # ---- 主体：左预览 / 右字段
        body = QHBoxLayout()
        body.setContentsMargins(16, 16, 16, 16)
        body.setSpacing(16)
        root.addLayout(body, 1)

        self._pdf_view = QPdfView()
        self._pdf_view.setObjectName("PdfPreview")
        self._pdf_view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        self._pdf_document: QPdfDocument | None = None
        body.addWidget(self._pdf_view, 12)  # ≈1.2fr

        right = QWidget()
        right_l = QVBoxLayout(right)
        right_l.setContentsMargins(0, 0, 0, 0)
        right_l.setSpacing(10)
        body.addWidget(right, 10)  # ≈1fr

        fields_title = QLabel("识别字段校验")
        fields_title.setFont(ui_font(11, 600))
        right_l.addWidget(fields_title)

        self._fields_area = QScrollArea()
        self._fields_area.setWidgetResizable(True)
        self._fields_area.setFrameShape(QFrame.Shape.NoFrame)
        self._fields_host = QWidget()
        self._fields_l = QVBoxLayout(self._fields_host)
        self._fields_l.setContentsMargins(0, 0, 8, 0)
        self._fields_l.setSpacing(8)
        self._fields_area.setWidget(self._fields_host)
        right_l.addWidget(self._fields_area, 1)

        self.load_document()

    # ------------------------------------------------------------------

    def load_document(self) -> None:
        """从 DB 拉取字段与验证结果，并加载 PDF 预览。"""
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
            error_reason = doc.error_reason
            fields = [(f.field_name, f.raw_value, f.normalized_value, f.parser) for f in doc.fields]
            verifications = [
                (v.field_name, v.primary_value, v.secondary_value, v.matched, v.review_status)
                for v in doc.verifications
            ]
            pdf_path = doc.file_path

        kind, text = _badge_for_status(status)
        status_row = QHBoxLayout()
        status_label = QLabel("识别状态：")
        status_label.setFont(ui_font(10))
        status_row.addWidget(status_label)
        badge = Badge(text, kind)
        badge.setFont(ui_font(9, 500))
        status_row.addWidget(badge)
        status_row.addStretch(1)
        self._fields_l.addLayout(status_row)

        if error_reason:
            reason = QLabel(error_reason)
            reason.setWordWrap(True)
            reason.setStyleSheet(
                "background: #fef2f2; border: 1px solid #fecaca; border-radius: 6px;"
                "color: #b91c1c; font-size: 12px; padding: 8px;"
            )
            self._fields_l.addWidget(reason)

        if not fields:
            self._add_hint("没有解析字段")
        else:
            for name, raw, normalized, parser in fields:
                self._add_field_row(name, raw, normalized, parser)

        if verifications:
            sep = QFrame()
            sep.setFixedHeight(1)
            sep.setStyleSheet("background: #e2e8f0;")
            self._fields_l.addWidget(sep)
            v_title = QLabel("交叉验证（pdfplumber）")
            v_title.setFont(ui_font(10, 600))
            self._fields_l.addWidget(v_title)
            for name, primary, secondary, matched, review in verifications:
                self._add_verification_row(name, primary, secondary, matched)

        self._fields_l.addStretch(1)

        if pdf_path and Path(pdf_path).exists():
            document = QPdfDocument(self)
            if document.load(pdf_path) == QPdfDocument.Error.None_:
                self._pdf_document = document
                self._pdf_view.setDocument(document)

    def _add_hint(self, text: str) -> None:
        label = QLabel(text)
        label.setStyleSheet("color: #94a3b8; font-size: 13px;")
        self._fields_l.addWidget(label)

    def _add_field_row(self, name: str, raw: str | None, normalized: str | None, parser: str) -> None:
        row = QFrame()
        row.setObjectName("Card")
        row_l = QVBoxLayout(row)
        row_l.setContentsMargins(10, 8, 10, 8)
        row_l.setSpacing(4)

        head = QHBoxLayout()
        name_label = QLabel(name)
        name_label.setFont(ui_font(10, 600))
        head.addWidget(name_label)
        head.addStretch(1)
        parser_label = QLabel(parser)
        parser_label.setObjectName("MutedText")
        head.addWidget(parser_label)
        row_l.addLayout(head)

        value_label = QLabel(str(normalized if normalized not in (None, "") else raw or "—"))
        value_label.setFont(ui_font(10))
        value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        value_label.setWordWrap(True)
        row_l.addWidget(value_label)

        if raw and normalized and raw != normalized:
            raw_label = QLabel(f"原始值：{raw}")
            raw_label.setObjectName("MutedText")
            raw_label.setWordWrap(True)
            row_l.addWidget(raw_label)

        self._fields_l.addWidget(row)

    def _add_verification_row(self, name: str, primary: str | None, secondary: str | None, matched: bool) -> None:
        row = QHBoxLayout()
        mark = QLabel("✓" if matched else "✗")
        mark.setStyleSheet(f"color: {'#16a34a' if matched else '#ef4444'}; font-weight: 600;")
        row.addWidget(mark)
        text = QLabel(f"{name}：{primary or '—'}  ↔  {secondary or '—'}")
        text.setFont(ui_font(10))
        row.addWidget(text, 1)
        self._fields_l.addLayout(row)
