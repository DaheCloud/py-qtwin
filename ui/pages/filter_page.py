"""页面 2：数据筛选与管理（对应 ui.html 的 #page-filter）。

从 SQLite 读取已解析文档，支持关键字/状态过滤、全选与批量导出、
点击复制单元格、查看详情、整行复制、删除（含审计日志）。
"""

from __future__ import annotations

import csv
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ui.styles import STATUS_BADGE_CLASS, ui_font
from ui.widgets.common import BadgeDelegate, CopyCellDelegate, Toast

COL_CHECK, COL_NAME, COL_CODE, COL_DATE, COL_AMOUNT, COL_STATUS, COL_ACTION = range(7)

_STATUS_TEXT = {
    "success": "准确",
    "manual_review": "可疑/待校验",
    "warning": "可疑/待校验",
    "failed": "解析失败",
}
_STATUS_TO_KIND = {"success": "success", "manual_review": "warning", "warning": "warning", "failed": "failed"}


def _status_text(status: str) -> str:
    return _STATUS_TEXT.get(status, status)


class FilterPage(QWidget):
    """已解析数据的管理与导出页。"""

    detail_requested = Signal(int, str)  # document_id, file_name

    def __init__(self, db_path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._db_path = db_path
        self._toast = Toast(self)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)

        # 过滤条卡片
        bar = QFrame()
        bar.setObjectName("Card")
        bar_l = QHBoxLayout(bar)
        bar_l.setContentsMargins(16, 14, 16, 14)
        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索文件名/合同编号...")
        self._search.setFixedWidth(220)
        self._search.returnPressed.connect(self.reload)
        bar_l.addWidget(self._search)

        self._status_filter = QComboBox()
        self._status_filter.addItem("全部识别状态", "")
        self._status_filter.addItem("准确", "success")
        self._status_filter.addItem("可疑/待校验", "review")
        bar_l.addWidget(self._status_filter)

        query_btn = QPushButton("查询")
        query_btn.setProperty("cssClass", "btn-primary")
        query_btn.clicked.connect(self.reload)
        bar_l.addWidget(query_btn)
        bar_l.addStretch(1)
        root.addWidget(bar)

        # 数据卡片
        card = QFrame()
        card.setObjectName("Card")
        card_l = QVBoxLayout(card)
        card_l.setContentsMargins(16, 16, 16, 16)
        card_l.setSpacing(12)

        head = QHBoxLayout()
        export_btn = QPushButton("📊 批量导出选中项至 Excel")
        export_btn.setProperty("cssClass", "btn-success")
        export_btn.clicked.connect(self._export_selected)
        head.addWidget(export_btn)
        head.addStretch(1)
        self._select_count_label = QLabel()
        self._select_count_label.setObjectName("MutedText")
        head.addWidget(self._select_count_label)
        card_l.addLayout(head)

        self._table = QTableWidget(0, 7)
        self._table.setHorizontalHeaderLabels(
            ["", "文件名", "合同编号", "开票日期", "金额", "状态", "操作"]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(36)  # 容纳行内按钮文字
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self._table.horizontalHeader()
        header.setMinimumSectionSize(40)
        header.setSectionResizeMode(COL_CHECK, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(COL_CHECK, 40)
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        # Interactive：默认宽度合理，且用户可拖动列边界自行调整
        for col, width in ((COL_CODE, 150), (COL_DATE, 120), (COL_AMOUNT, 120), (COL_STATUS, 110), (COL_ACTION, 190)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            self._table.setColumnWidth(col, width)

        self._copy_delegate = CopyCellDelegate(self._table)
        self._copy_delegate.cell_copied.connect(lambda t: self._toast.show_message(f"已复制：{t}"))
        for col in (COL_NAME, COL_CODE, COL_DATE, COL_AMOUNT):
            self._table.setItemDelegateForColumn(col, self._copy_delegate)
        self._table.setItemDelegateForColumn(COL_STATUS, BadgeDelegate(self._table))
        card_l.addWidget(self._table, 1)
        root.addWidget(card, 1)

        self.reload()

    # ------------------------------------------------------------- 数据加载

    def reload(self) -> None:
        """按过滤条件从 DB 重新加载文档列表。"""
        from sqlalchemy import select

        from database.db import get_engine, init_db, make_session_factory
        from models.document import Document, ExtractedField

        engine = get_engine(self._db_path)
        init_db(engine)
        factory = make_session_factory(engine)

        keyword = self._search.text().strip()
        status = self._status_filter.currentData()

        with factory() as session:
            stmt = select(Document).order_by(Document.imported_at.desc())
            docs = list(session.scalars(stmt))
            rows = []
            for doc in docs:
                if keyword and keyword.lower() not in doc.file_name.lower():
                    fields = {f.field_name: f.normalized_value for f in doc.fields}
                    if not any(keyword.lower() in str(v).lower() for v in fields.values()):
                        continue
                if status == "review" and doc.status not in ("manual_review", "warning"):
                    continue
                if status and status != "review" and doc.status != status:
                    continue
                fields = {f.field_name: f.normalized_value for f in doc.fields}
                rows.append((doc.id, doc.file_name, fields))

        self._table.setRowCount(0)
        for doc_id, file_name, fields in rows:
            self._append_row(doc_id, file_name, fields)
        self._update_select_count()
        if not rows:
            self._toast.show_message("没有符合条件的数据")

    def _append_row(self, doc_id: int, file_name: str, fields: dict[str, str | None]) -> None:
        r = self._table.rowCount()
        self._table.insertRow(r)

        check = QTableWidgetItem()
        check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        check.setCheckState(Qt.CheckState.Unchecked)
        self._table.setItem(r, COL_CHECK, check)

        self._table.setItem(r, COL_NAME, QTableWidgetItem(file_name))
        self._table.setItem(r, COL_CODE, QTableWidgetItem(str(fields.get("contract_no") or "—")))
        self._table.setItem(r, COL_DATE, QTableWidgetItem(str(fields.get("sign_date") or "—")))
        amount = fields.get("amount")
        self._table.setItem(r, COL_AMOUNT, QTableWidgetItem(f"￥{amount}" if amount else "—"))
        self._table.item(r, COL_NAME).setData(Qt.ItemDataRole.UserRole, doc_id)

        status_item = QTableWidgetItem(_status_text("success"))
        self._table.setItem(r, COL_STATUS, status_item)

        actions = QWidget()
        a_l = QHBoxLayout(actions)
        a_l.setContentsMargins(4, 2, 4, 2)
        a_l.setSpacing(2)
        for text, handler, cls in (
            ("查看", self._view_row, "btn-default"),
            ("复制", self._copy_row, "btn-default"),
            ("删除", self._delete_row, "btn-danger-text"),
        ):
            btn = QPushButton(text)
            btn.setProperty("cssClass", cls)
            btn.setFixedHeight(24)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, rr=r: handler(rr))
            a_l.addWidget(btn)
        self._table.setCellWidget(r, COL_ACTION, actions)
        actions.setFixedHeight(28)
        actions.move(actions.x(), max(0, (self._table.rowHeight(r) - 28) // 2))
        self._refresh_row_status(r)

    def _refresh_row_status(self, row: int) -> None:
        """按 DB 最新状态刷新行的状态徽章与值。"""
        from sqlalchemy import select

        from database.db import get_engine, make_session_factory
        from models.document import Document, ExtractedField

        doc_id = self._table.item(row, COL_NAME).data(Qt.ItemDataRole.UserRole)
        engine = get_engine(self._db_path)
        factory = make_session_factory(engine)
        with factory() as session:
            doc = session.get(Document, doc_id)
            if doc is None:
                return
            fields = {f.field_name: f.normalized_value for f in doc.fields}
            status = doc.status
        kind = _STATUS_TO_KIND.get(status, "info")
        status_item = self._table.item(row, COL_STATUS)
        status_item.setText(_status_text(status))
        status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        status_item.setData(Qt.ItemDataRole.UserRole, kind)

    # ------------------------------------------------------------- 行为

    def _selected_doc_ids(self) -> list[int]:
        ids = []
        for r in range(self._table.rowCount()):
            if self._table.item(r, COL_CHECK).checkState() == Qt.CheckState.Checked:
                ids.append(self._table.item(r, COL_NAME).data(Qt.ItemDataRole.UserRole))
        return ids

    def _update_select_count(self) -> None:
        n = len(self._selected_doc_ids())
        self._select_count_label.setText(f"已选择 <b style='color:#2563eb'>{n}</b> 项")

    def _export_selected(self) -> None:
        ids = set(self._selected_doc_ids())
        if not ids:
            self._toast.show_message("请先勾选要导出的行")
            return
        default_name = f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        path, _ = QFileDialog.getSaveFileName(self, "导出 CSV", default_name, "CSV 文件 (*.csv)")
        if not path:
            return
        from sqlalchemy import select

        from database.db import get_engine, make_session_factory
        from models.document import Document, ExtractedField

        engine = get_engine(self._db_path)
        factory = make_session_factory(engine)
        with factory() as session:
            stmt = select(Document).where(Document.id.in_(ids))
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["文件名", "合同编号", "开票日期", "金额", "状态", "导入时间"])
                for doc in session.scalars(stmt):
                    fields = {fd.field_name: fd.normalized_value for fd in doc.fields}
                    writer.writerow(
                        [
                            doc.file_name,
                            fields.get("contract_no") or "",
                            fields.get("sign_date") or "",
                            fields.get("amount") or "",
                            _status_text(doc.status),
                            doc.imported_at.strftime("%Y-%m-%d %H:%M:%S"),
                        ]
                    )
        self._toast.show_message(f"已导出 {len(ids)} 条数据")

    def _view_row(self, row: int) -> None:
        doc_id = self._table.item(row, COL_NAME).data(Qt.ItemDataRole.UserRole)
        self.detail_requested.emit(doc_id, self._table.item(row, COL_NAME).text())

    def _copy_row(self, row: int) -> None:
        cells = [self._table.item(row, c).text() for c in (COL_NAME, COL_CODE, COL_DATE, COL_AMOUNT)]
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText("\t".join(cells))
        self._toast.show_message("整行数据已复制")

    def _delete_row(self, row: int) -> None:
        doc_id = self._table.item(row, COL_NAME).data(Qt.ItemDataRole.UserRole)
        name = self._table.item(row, COL_NAME).text()
        answer = QMessageBox.question(self, "确认删除", f"确认删除「{name}」及其解析数据？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        from sqlalchemy import select

        from database.db import get_engine, make_session_factory
        from models.document import AuditLog, Document

        engine = get_engine(self._db_path)
        factory = make_session_factory(engine)
        with factory() as session:
            doc = session.get(Document, doc_id)
            if doc is not None:
                session.delete(doc)
                session.add(AuditLog(action="delete_document", detail=f"id={doc_id} name={name}"))
                session.commit()
        self._table.removeRow(row)
        self._update_select_count()
        self._toast.show_message("已删除")
