"""页面 2：数据筛选与管理（对应 ui.html 的 #page-filter）。

从 SQLite 读取已解析文档，支持关键字/状态过滤、全选、批量导出与批量删除、
点击复制单元格、查看详情、整行复制、删除（含审计日志）。
"""

from __future__ import annotations

import re
from datetime import datetime

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal, QSize
from PySide6.QtGui import (
    QColor,
    QIcon,
    QPainter,
    QPixmap,
    QPen,
    QStandardItem,
    QStandardItemModel,
)
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

from ui.field_labels import field_label
from ui.styles import STATUS_BADGE_CLASS, ui_font
from ui.widgets.common import BadgeDelegate, CopyCellDelegate, Toast

# 数据列定义：(表头, 主字段, 回退字段(旧合同数据兼容), 默认宽度, 是否金额列)
# 表头中文名统一取自 ui.field_labels（与详情弹窗共用一份映射，避免漂移）
DATA_COLUMNS: list[tuple[str, str, str | None, int, bool]] = [
    (field_label("invoice_no"), "invoice_no", "contract_no", 150, False),
    (field_label("invoice_date"), "invoice_date", "sign_date", 100, False),
    (field_label("buyer_name"), "buyer_name", "customer_name", 150, False),
    (field_label("buyer_tax_no"), "buyer_tax_no", None, 160, False),
    (field_label("seller_name"), "seller_name", None, 150, False),
    (field_label("seller_tax_no"), "seller_tax_no", None, 160, False),
    (field_label("item_name"), "item_name", None, 180, False),
    (field_label("spec_model"), "spec_model", None, 100, False),
    (field_label("unit"), "unit", None, 55, False),
    (field_label("quantity"), "quantity", None, 70, False),
    (field_label("unit_price"), "unit_price", None, 100, True),
    (field_label("construction_site"), "construction_site", None, 180, False),
    (field_label("project_name"), "project_name", None, 180, False),
    (field_label("tax_rate"), "tax_rate", None, 55, False),
    (field_label("amount"), "amount", None, 100, True),
    (field_label("tax_amount"), "tax_amount", None, 90, True),
    (field_label("total_amount"), "total_amount", None, 100, True),
    (field_label("item_rows"), "item_rows", None, 80, False),
]
COL_CHECK, COL_NAME = 0, 1
COL_DATA_START = 2
COL_STATUS = COL_DATA_START + len(DATA_COLUMNS)
TOTAL_COLS = COL_STATUS + 1
# 操作列作为右侧冻结面板（独立表格，不随主表横向滚动）
ACTION_COL_WIDTH = 190

_KEY_HIDDEN_COLS = "filter/hidden_columns"
_KEY_AUTO_FIT = "filter/auto_fit_columns"

_STATUS_TEXT = {
    "success": "准确",
    "manual_review": "可疑/待校验",
    "warning": "可疑/待校验",
    "failed": "解析失败",
    "needs_ocr": "等待 OCR",
}
_STATUS_TO_KIND = {
    "success": "success",
    "manual_review": "warning",
    "warning": "warning",
    "failed": "failed",
    "needs_ocr": "info",
}


def _status_text(status: str) -> str:
    return _STATUS_TEXT.get(status, status)


# 日期文本 → 年月：兼容 "2026-09-09"、"2026年9月9日"、"2026/09/09"
_MONTH_RE = re.compile(r"(\d{4})[-/年.](\d{1,2})")


def year_month(value: str | None) -> str:
    """日期文本 → 年月 "YYYY-MM"；不可解析返回空串。"""
    match = _MONTH_RE.search(str(value or ""))
    return f"{match.group(1)}-{int(match.group(2)):02d}" if match else ""


def record_month(fields: dict[str, str | None]) -> str:
    """记录的开票年月：优先 invoice_date，兼容旧合同数据的 sign_date。"""
    return year_month(fields.get("invoice_date") or fields.get("sign_date"))


def _check_icon(checked: bool) -> QIcon:
    """自绘勾选图标：未选灰框白底；选中蓝底白勾（完全自绘，不受系统控件渲染影响）。

    图形四周留足 2px 透明边距，DPI 缩放重采样时也不会切到边框。
    """
    size = 16
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    rect = QRectF(2, 2, size - 4, size - 4)
    if checked:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#2563eb"))
        p.drawRoundedRect(rect, 4, 4)
        pen = QPen(QColor("#ffffff"), 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.drawLine(QPointF(4.5, 8.5), QPointF(7, 11))
        p.drawLine(QPointF(7, 11), QPointF(11.5, 5))
    else:
        p.setPen(QPen(QColor("#94a3b8"), 1))
        p.setBrush(QColor("#ffffff"))
        p.drawRoundedRect(rect, 4, 4)
    p.end()
    return QIcon(pm)


class FilterPage(QWidget):
    """已解析数据的管理与导出页。"""

    detail_requested = Signal(int, str)  # document_id, file_name

    def __init__(self, db_path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._db_path = db_path
        self._toast = Toast(self)
        # 批量勾选（全选/取消全选）期间挂起"已选择 N 项"刷新，避免逐行触发 O(n²)
        self._suspend_select_count = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)

        # 过滤条卡片
        bar = QFrame()
        bar.setObjectName("Card")
        bar_l = QHBoxLayout(bar)
        bar_l.setContentsMargins(16, 14, 16, 14)
        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索文件名 / 任意识别字段...")
        self._search.setFixedWidth(220)
        self._search.returnPressed.connect(self.reload)
        bar_l.addWidget(self._search)

        self._status_filter = QComboBox()
        self._status_filter.addItem("全部识别状态", "")
        self._status_filter.addItem("准确", "success")
        self._status_filter.addItem("可疑/待校验", "review")
        bar_l.addWidget(self._status_filter)

        # 开票年月过滤：选项在 reload 时按库内数据动态统计（只到年月）
        self._month_filter = QComboBox()
        self._month_filter.addItem("全部月份", "")
        self._month_filter.setMinimumWidth(120)
        self._month_filter.currentIndexChanged.connect(self.reload)
        bar_l.addWidget(self._month_filter)

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

        # 一键确认：勾选"可疑/待校验"的记录后批量标记为「准确」
        self._confirm_btn = QPushButton("✓ 一键确认选中项")
        self._confirm_btn.setProperty("cssClass", "btn-success")
        self._confirm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._confirm_btn.setToolTip("把勾选的「可疑/待校验」记录批量标记为「准确」（记录审计日志）")
        self._confirm_btn.clicked.connect(self._confirm_selected)
        head.addWidget(self._confirm_btn)

        delete_btn = QPushButton("🗑 批量删除选中项")
        delete_btn.setProperty("cssClass", "btn-danger")
        delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        delete_btn.clicked.connect(self._delete_selected)
        head.addWidget(delete_btn)
        self._cols_combo = self._build_cols_combo()
        head.addWidget(self._cols_combo)
        self._auto_fit_btn = QPushButton("自适应列宽：关")
        self._auto_fit_btn.setCheckable(True)
        self._auto_fit_btn.setProperty("cssClass", "btn-auto-fit")
        self._auto_fit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._auto_fit_btn.setChecked(self._auto_fit_enabled())
        self._auto_fit_btn.toggled.connect(self._toggle_auto_fit)
        head.addWidget(self._auto_fit_btn)
        head.addStretch(1)
        self._select_count_label = QLabel()
        self._select_count_label.setObjectName("MutedText")
        head.addWidget(self._select_count_label)
        card_l.addLayout(head)

        self._table = QTableWidget(0, TOTAL_COLS)
        self._table.setHorizontalHeaderLabels(
            ["", "文件名", *[c[0] for c in DATA_COLUMNS], "状态"]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(48)  # 与上传页行高统一
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self._table.horizontalHeader()
        header.setMinimumSectionSize(40)
        header.setSectionResizeMode(COL_CHECK, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(COL_CHECK, 40)
        # 文件名列也固定宽，不自动拉伸——列宽完全手动控制
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Interactive)
        self._table.setColumnWidth(COL_NAME, 220)
        # 数据列 Interactive：默认宽度合理，且用户可拖动列边界自行调整
        for i, (_, _, _, width, _) in enumerate(DATA_COLUMNS):
            col = COL_DATA_START + i
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            self._table.setColumnWidth(col, width)
        header.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeMode.Interactive)
        self._table.setColumnWidth(COL_STATUS, 100)

        # 操作列：右侧冻结面板，主表横向滚动时保持可见
        self._action_table = QTableWidget(0, 1)
        self._action_table.setHorizontalHeaderLabels(["操作"])
        self._action_table.setObjectName("ActionTable")
        self._action_table.verticalHeader().setVisible(False)
        self._action_table.verticalHeader().setDefaultSectionSize(48)
        self._action_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._action_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._action_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._action_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._action_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._action_table.setColumnWidth(0, ACTION_COL_WIDTH)
        # 固定面板总宽 = 列宽，否则布局会把表格拉宽、列右侧多出一片空白
        self._action_table.setFixedWidth(ACTION_COL_WIDTH)
        action_header = self._action_table.horizontalHeader()
        action_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        # 主表垂直滚动 → 冻结面板同步；主表选行 → 面板高亮同步
        self._table.verticalScrollBar().valueChanged.connect(
            self._action_table.verticalScrollBar().setValue
        )
        self._table.currentCellChanged.connect(
            lambda cr, _cc, _pr, _pc: self._action_table.selectRow(cr) if cr >= 0 else None
        )

        self._copy_delegate = CopyCellDelegate(self._table)
        self._copy_delegate.cell_copied.connect(lambda t: self._toast.show_message(f"已复制：{t}"))
        for col in range(COL_NAME, COL_DATA_START + len(DATA_COLUMNS)):
            self._table.setItemDelegateForColumn(col, self._copy_delegate)
        self._table.setItemDelegateForColumn(COL_STATUS, BadgeDelegate(self._table))

        table_area = QWidget()
        table_area_l = QHBoxLayout(table_area)
        table_area_l.setContentsMargins(0, 0, 0, 0)
        table_area_l.setSpacing(0)
        table_area_l.addWidget(self._table, 1)
        table_area_l.addWidget(self._action_table)
        card_l.addWidget(table_area, 1)
        root.addWidget(card, 1)

        # ---- 表头全选框（叠加在勾选列表头中央，自绘样式）
        self._select_all_btn = QPushButton()
        self._select_all_btn.setCheckable(True)
        self._select_all_btn.setProperty("cssClass", "row-check")
        self._select_all_btn.setFixedSize(18, 18)
        self._select_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._select_all_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._select_all_btn.setToolTip("全选 / 取消全选")
        self._select_all_btn.toggled.connect(self._select_all_rows)
        self._select_all_btn.setParent(self._table.horizontalHeader())
        self._select_all_btn.show()
        header.sectionResized.connect(lambda *_: self._position_select_all())
        self._table.horizontalScrollBar().valueChanged.connect(lambda *_: self._position_select_all())
        self._position_select_all()

        self._apply_hidden_columns()  # 恢复上次的列显示设置
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
        # 多关键词（空格分隔）全部命中才显示；金额关键词自动兼容 ￥/¥ 符号
        keywords = [k.lower().replace("￥", "").replace("¥", "") for k in keyword.split()] if keyword else []
        status = self._status_filter.currentData()

        with factory() as session:
            stmt = select(Document).order_by(Document.imported_at.desc())
            records = [
                (doc.id, doc.file_name, {f.field_name: f.normalized_value for f in doc.fields}, doc.status)
                for doc in session.scalars(stmt)
            ]

        # 先统计库内有哪些开票年月（下拉选项），再按当前选择过滤
        self._sync_month_options(
            sorted({record_month(fields) for _, _, fields, _ in records} - {""}, reverse=True)
        )
        month = self._month_filter.currentData() or ""

        rows = []
        for doc_id, file_name, fields, doc_status in records:
            if keywords:
                hay = file_name.lower() + "\n" + "\n".join(
                    str(v).lower() for v in fields.values() if v
                )
                if not all(k in hay for k in keywords):
                    continue
            if status == "review" and doc_status not in ("manual_review", "warning"):
                continue
            if status and status != "review" and doc_status != status:
                continue
            if month and record_month(fields) != month:
                continue
            rows.append((doc_id, file_name, fields))

        self._table.setRowCount(0)
        self._action_table.setRowCount(0)
        for doc_id, file_name, fields in rows:
            self._append_row(doc_id, file_name, fields)
        if self._auto_fit_enabled():
            self._auto_fit_columns()  # 开关开启时：数据刷新后按内容自动调整列宽
        self._update_select_count()
        if not rows:
            self._toast.show_message("没有符合条件的数据")

    def _sync_month_options(self, months: list[str]) -> None:
        """刷新"开票年月"下拉选项：保留当前选择，选项已消失时回到"全部月份"。

        blockSignals 避免 clear/addItem/setCurrentIndex 触发 currentIndexChanged
        递归回 reload（统计与过滤必须在同一次刷新里完成）。
        """
        current = self._month_filter.currentData() or ""
        self._month_filter.blockSignals(True)
        self._month_filter.clear()
        self._month_filter.addItem("全部月份", "")
        for month in months:
            year, _, mon = month.partition("-")
            self._month_filter.addItem(f"{year}年{mon}月", month)
        index = self._month_filter.findData(current)
        self._month_filter.setCurrentIndex(index if index >= 0 else 0)
        self._month_filter.blockSignals(False)

    def _append_row(self, doc_id: int, file_name: str, fields: dict[str, str | None]) -> None:
        r = self._table.rowCount()
        self._table.insertRow(r)

        # 勾选：透明按钮铺满整格 + 居中自绘图标（图标绘制在按钮中心，绝不会被裁剪）
        check_btn = QPushButton()
        check_btn.setCheckable(True)
        check_btn.setProperty("cssClass", "row-check-icon")
        check_btn.setFlat(True)
        check_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        check_btn.setIcon(_check_icon(False))
        check_btn.setIconSize(QSize(16, 16))
        check_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        check_btn.toggled.connect(lambda on, b=check_btn: self._on_row_checked(on, b))
        self._table.setCellWidget(r, COL_CHECK, check_btn)

        self._table.setItem(r, COL_NAME, QTableWidgetItem(file_name))
        self._table.item(r, COL_NAME).setData(Qt.ItemDataRole.UserRole, doc_id)
        # 全部识别字段列；主字段缺失时回退旧合同字段
        for i, (_, key, fallback, _, is_money) in enumerate(DATA_COLUMNS):
            col = COL_DATA_START + i
            v = fields.get(key) or (fields.get(fallback) if fallback else None)
            text = f"￥{v}" if (v and is_money) else (str(v) if v not in (None, "") else "—")
            item = QTableWidgetItem(text)
            item.setToolTip(text)  # 列宽不足时悬停查看完整内容
            self._table.setItem(r, col, item)

        status_item = QTableWidgetItem(_status_text("success"))
        self._table.setItem(r, COL_STATUS, status_item)

        # 操作按钮写入右侧冻结面板（与主表行号一一对应）
        ar = self._action_table.rowCount()
        self._action_table.insertRow(ar)
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
            btn.setFixedHeight(32)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            # handler/r 均通过默认参数绑定，避免闭包共享循环变量导致全部执行删除
            btn.clicked.connect(lambda _=False, rr=r, h=handler: h(rr))
            a_l.addWidget(btn)
        self._action_table.setCellWidget(ar, 0, actions)
        actions.setFixedHeight(36)
        actions.move(actions.x(), max(0, (self._action_table.rowHeight(ar) - 36) // 2))
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

    def _position_select_all(self) -> None:
        """把全选框定位到勾选列表头的中央（随横向滚动移动）。"""
        header = self._table.horizontalHeader()
        x = self._table.columnViewportPosition(COL_CHECK) + self._table.columnWidth(COL_CHECK) // 2 - 10
        self._select_all_btn.move(max(2, x), max(2, (header.height() - 20) // 2))

    def _on_row_checked(self, checked: bool, button: QPushButton) -> None:
        """行勾选：换图标并实时刷新"已选择 N 项"。

        批量勾选期间挂起刷新（否则每行都全表统计一次，大数据量下是 O(n²)），
        由 _select_all_rows 结束时统一刷新一次。
        """
        button.setIcon(_check_icon(checked))
        if not self._suspend_select_count:
            self._update_select_count()

    def _select_all_rows(self, checked: bool) -> None:
        """全选 / 取消全选。"""
        self._suspend_select_count = True
        try:
            for r in range(self._table.rowCount()):
                btn = self._table.cellWidget(r, COL_CHECK)
                if isinstance(btn, QPushButton):
                    btn.setChecked(checked)
        finally:
            self._suspend_select_count = False
        self._update_select_count()

    # ------------------------------------------------------------- 列显示控制

    def _auto_fit_enabled(self) -> bool:
        """自适应列宽开关状态（持久化，默认关闭）。"""
        from ui.pages.settings_page import load_settings

        return load_settings().value(
            _KEY_AUTO_FIT, False, type=bool
        )

    def _sync_auto_fit_btn(self) -> None:
        self._auto_fit_btn.setText("自适应列宽：开" if self._auto_fit_btn.isChecked() else "自适应列宽：关")

    def _toggle_auto_fit(self, checked: bool) -> None:
        """切换自适应开关：开启时立即执行一次自适应，状态持久化。"""
        from ui.pages.settings_page import load_settings

        load_settings().setValue(_KEY_AUTO_FIT, checked)
        self._sync_auto_fit_btn()
        if checked:
            self._auto_fit_columns()
            self._toast.show_message("已开启：数据刷新时自动按内容调整列宽")
        else:
            self._toast.show_message("已关闭：列宽完全手动控制")

    def _auto_fit_columns(self) -> None:
        """所有可见数据列按内容自适应宽度，并留出单元格内边距余量。"""
        for i in range(len(DATA_COLUMNS)):
            col = COL_DATA_START + i
            if self._table.isColumnHidden(col):
                continue
            self._table.resizeColumnToContents(col)
            self._table.setColumnWidth(col, self._table.columnWidth(col) + 20)

    def _hidden_columns(self) -> set[str]:
        """读取持久化的隐藏列（字段名集合）。"""
        from ui.pages.settings_page import load_settings

        raw = str(load_settings().value(_KEY_HIDDEN_COLS, ""))
        return {s for s in raw.split(",") if s}

    def _save_hidden_columns(self, hidden: set[str]) -> None:
        from ui.pages.settings_page import load_settings

        load_settings().setValue(
            _KEY_HIDDEN_COLS, ",".join(sorted(hidden))
        )

    def _apply_hidden_columns(self) -> None:
        """启动时按持久化设置隐藏列。"""
        hidden = self._hidden_columns()
        for i, (_, key, _, _, _) in enumerate(DATA_COLUMNS):
            self._table.setColumnHidden(COL_DATA_START + i, key in hidden)

    def _build_cols_combo(self) -> QComboBox:
        """多选下拉：展开后连续勾选多项（下拉不收起），选择即时生效并持久化。"""
        combo = QComboBox()
        self._cols_combo = combo  # 尽早绑定，构建过程中的事件/文本刷新可用
        combo.setObjectName("ColsSelect")
        self._cols_model = QStandardItemModel(combo)
        hidden = self._hidden_columns()
        all_item = QStandardItem("全部隐藏" if not hidden else "全部显示")
        all_item.setCheckable(False)
        all_item.setEditable(False)
        self._cols_model.appendRow(all_item)
        for title, key, _, _, _ in DATA_COLUMNS:
            item = QStandardItem(title)
            item.setCheckable(True)
            item.setEditable(False)
            item.setCheckState(Qt.CheckState.Unchecked if key in hidden else Qt.CheckState.Checked)
            self._cols_model.appendRow(item)
        combo.setModel(self._cols_model)
        combo.setCurrentIndex(-1)
        combo.view().viewport().installEventFilter(self)
        self._refresh_cols_text()
        return combo

    def _refresh_cols_text(self) -> None:
        """按下拉里各列的勾选状态刷新显示文本（不依赖表格）。"""
        visible_n = sum(
            1
            for i in range(len(DATA_COLUMNS))
            if self._cols_model.item(i + 1).checkState() == Qt.CheckState.Checked
        )
        self._cols_combo.setPlaceholderText(f"显示列 ({visible_n}/{len(DATA_COLUMNS)})")
        self._cols_model.item(0).setText("全部隐藏" if visible_n == len(DATA_COLUMNS) else "全部显示")

    def eventFilter(self, obj, event) -> bool:
        """勾选列表项鼠标松开时切换勾选，并吞掉该事件使下拉保持展开。"""
        combo = getattr(self, "_cols_combo", None)
        if combo is None or obj is not combo.view().viewport():
            return super().eventFilter(obj, event)
        if event.type() != QEvent.Type.MouseButtonRelease:
            return super().eventFilter(obj, event)
        from PySide6.QtCore import QModelIndex

        view = combo.view()
        idx: QModelIndex = view.indexAt(event.position().toPoint())
        if idx.isValid():
            if idx.row() == 0:
                # 首项：状态切换——有列隐藏时全部显示，全部可见时全部隐藏
                show_all = any(
                    self._cols_model.item(i + 1).checkState() != Qt.CheckState.Checked
                    for i in range(len(DATA_COLUMNS))
                )
                for i in range(len(DATA_COLUMNS)):
                    self._cols_model.item(i + 1).setCheckState(
                        Qt.CheckState.Checked if show_all else Qt.CheckState.Unchecked
                    )
                    self._table.setColumnHidden(COL_DATA_START + i, not show_all)
                self._save_hidden_columns(set() if show_all else {key for _, key, _, _, _ in DATA_COLUMNS})
            else:
                item = self._cols_model.itemFromIndex(idx)
                data_row = idx.row() - 1  # 首项为功能项
                on = item.checkState() != Qt.CheckState.Checked
                item.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
                self._set_column_visible(DATA_COLUMNS[data_row][1], COL_DATA_START + data_row, on)
            self._refresh_cols_text()
        return True  # 阻止下拉收起

    def _set_column_visible(self, key: str, col: int, visible: bool) -> None:
        self._table.setColumnHidden(col, not visible)
        hidden = self._hidden_columns()
        if visible:
            hidden.discard(key)
        else:
            hidden.add(key)
        self._save_hidden_columns(hidden)

    def _selected_doc_ids(self) -> list[int]:
        ids = []
        for r in range(self._table.rowCount()):
            btn = self._table.cellWidget(r, COL_CHECK)
            if isinstance(btn, QPushButton) and btn.isChecked():
                ids.append(self._table.item(r, COL_NAME).data(Qt.ItemDataRole.UserRole))
        return ids

    def _update_select_count(self) -> None:
        """刷新"已选择 N 项"（勾选/取消勾选/全选/刷新数据后都会走到这里）。

        列表为空时不显示：空列表上挂一条"已选择 0 项"没有意义（此时
        过滤条件提示已经由 Toast 给出）。
        """
        n = len(self._selected_doc_ids())
        self._select_count_label.setText(f"已选择 <b style='color:#2563eb'>{n}</b> 项")
        self._select_count_label.setVisible(self._table.rowCount() > 0)

    def _export_selected(self) -> None:
        ids = set(self._selected_doc_ids())
        if not ids:
            self._toast.show_message("请先勾选要导出的行")
            return
        default_name = f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        path, _ = QFileDialog.getSaveFileName(self, "导出 Excel", default_name, "Excel 工作簿 (*.xlsx)")
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        from sqlalchemy import select

        from database.db import get_engine, make_session_factory
        from models.document import Document

        # 税号列强制文本（防止 Excel 把 18 位信用代码转成科学计数法丢精度）；
        # 金额列尽量写成数字单元格便于后续计算。
        TAX_NO_KEYS = {"buyer_tax_no", "seller_tax_no"}
        AMOUNT_KEYS = {"amount", "tax_amount", "total_amount", "unit_price"}
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Alignment, Font
            from openpyxl.utils import get_column_letter
        except ImportError:
            QMessageBox.warning(self, "缺少依赖", "未安装 openpyxl，无法导出 Excel。请执行：pip install openpyxl")
            return

        engine = get_engine(self._db_path)
        factory = make_session_factory(engine)

        # 仅「准确」(success) 数据可导出；勾选中含「可疑/待校验」时需确认
        with factory() as session:
            status_rows = session.execute(
                select(Document.id, Document.status).where(Document.id.in_(ids))
            ).all()
        status_map = dict(status_rows)
        ok_ids = {i for i, s in status_map.items() if s == "success"}
        suspect_n = len(ids) - len(ok_ids)
        if not ok_ids:
            self._toast.show_message("勾选的数据中没有「准确」状态的记录，无法导出")
            return
        if suspect_n:
            answer = QMessageBox.question(
                self,
                "包含可疑数据",
                f"勾选中包含 {suspect_n} 条「可疑/待校验」数据，\n"
                f"将仅导出 {len(ok_ids)} 条「准确」数据。是否继续？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        with factory() as session:
            stmt = select(Document).where(Document.id.in_(ok_ids))
            # 导出跟随当前可见列（所见即所得）
            visible = [
                (title, key, fallback)
                for i, (title, key, fallback, _, _) in enumerate(DATA_COLUMNS)
                if not self._table.isColumnHidden(COL_DATA_START + i)
            ]
            headers = ["文件名", *[t for t, _, _ in visible]]
            wb = Workbook()
            ws = wb.active
            ws.title = "解析结果"
            ws.append(headers)
            for col in range(1, len(headers) + 1):
                c = ws.cell(row=1, column=col)
                c.font = Font(bold=True)
                c.alignment = Alignment(horizontal="center")
            ws.freeze_panes = "A2"
            for i, h in enumerate(headers, start=1):
                width = 32 if i == 1 else max(12, min(40, len(h) * 2 + 6))
                ws.column_dimensions[get_column_letter(i)].width = width

            for doc in session.scalars(stmt):
                fields = {fd.field_name: fd.normalized_value for fd in doc.fields}
                row_values: list[object] = []
                for _, key, fallback in visible:
                    v = fields.get(key) or (fields.get(fallback) if fallback else None)
                    text = str(v) if v not in (None, "") else ""
                    row_values.append(text if text else None)
                ws.append([doc.file_name, *row_values])

            # 数据行写完后，按列统一设置格式
            last_row = ws.max_row
            for idx, (_, key, _) in enumerate(visible):
                col = 2 + idx  # Excel 列号：A=文件名，B 起为数据列
                letter = get_column_letter(col)
                if key in TAX_NO_KEYS:
                    for r in range(2, last_row + 1):
                        ws[f"{letter}{r}"].number_format = "@"
                elif key in AMOUNT_KEYS:
                    for r in range(2, last_row + 1):
                        cell = ws[f"{letter}{r}"]
                        if isinstance(cell.value, str) and cell.value:
                            try:
                                cell.value = float(cell.value.replace(",", ""))
                                cell.number_format = "#,##0.00"
                            except ValueError:
                                pass
            try:
                wb.save(path)
            except OSError as e:
                QMessageBox.warning(self, "导出失败", f"无法写入文件（可能正被 Excel 打开）：\n{e}")
                return
        self._toast.show_message(f"已导出 {len(ok_ids)} 条「准确」数据")

    def _confirm_selected(self) -> None:
        """一键确认：把勾选的「可疑/待校验」记录批量标记为「准确」。

        只处理可疑状态：已经准确的不必再确认，解析失败的关键字段可能缺失，
        不适合一键放行（需在详情弹窗逐项核对）——跳过的条目会在提示里说明。
        落库语义与详情弹窗的「确认无误」一致（见 services/review_service.py）。
        """
        ids = self._selected_doc_ids()
        if not ids:
            self._toast.show_message("请先勾选要确认的行")
            return

        from database.db import get_engine, make_session_factory
        from services.review_service import confirm_documents

        engine = get_engine(self._db_path)
        factory = make_session_factory(engine)

        # 先试算：确认框里明确写出"将确认几条、跳过几条"
        with factory() as session:
            preview = confirm_documents(session, ids, source="filter_page", dry_run=True)
        if not preview.confirmed:
            self._toast.show_message("勾选的数据中没有「可疑/待校验」记录，无需确认")
            return

        message = (
            f"确认选中的 {preview.confirmed_count} 条「可疑/待校验」数据无误？\n"
            "确认后状态变为「准确」，并记录审计日志。"
        )
        if preview.skipped:
            message += f"\n（另有 {len(preview.skipped)} 条非可疑状态，将跳过）"
        if preview.missing:
            message += f"\n（另有 {len(preview.missing)} 条记录已不存在，将跳过）"
        answer = QMessageBox.question(self, "确认无误", message)
        if answer != QMessageBox.StandardButton.Yes:
            return

        with factory() as session:
            report = confirm_documents(session, preview.confirmed, source="filter_page")
            session.commit()

        self._select_all_btn.setChecked(False)  # 重置表头全选，避免刷新后勾选态残留
        self.reload()
        self._toast.show_message(
            f"已确认 {report.confirmed_count} 条，状态更新为「准确」"
            + (f"（顺带复核 {report.confirmed_fields} 个字段）" if report.confirmed_fields else "")
        )

    def _view_row(self, row: int) -> None:
        doc_id = self._table.item(row, COL_NAME).data(Qt.ItemDataRole.UserRole)
        self.detail_requested.emit(doc_id, self._table.item(row, COL_NAME).text())

    def _copy_row(self, row: int) -> None:
        cols = [COL_NAME, *range(COL_DATA_START, COL_DATA_START + len(DATA_COLUMNS))]
        cells = [self._table.item(row, c).text() for c in cols]
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText("\t".join(cells))
        self._toast.show_message("整行数据已复制")

    def _delete_selected(self) -> None:
        """一键删除勾选的全部记录（含级联的字段/校验数据，逐条写审计日志）。

        配合表头全选框使用即"删除全部"；确认后重新加载列表，保持与数据库一致。
        """
        ids = self._selected_doc_ids()
        if not ids:
            self._toast.show_message("请先勾选要删除的行")
            return
        answer = QMessageBox.question(
            self,
            "确认删除",
            f"确认删除选中的 {len(ids)} 条记录及其解析数据？\n删除后可用原 PDF 重新导入。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        from sqlalchemy import select

        from database.db import get_engine, make_session_factory
        from models.document import AuditLog, Document

        engine = get_engine(self._db_path)
        factory = make_session_factory(engine)
        deleted = 0
        with factory() as session:
            for doc in session.scalars(select(Document).where(Document.id.in_(ids))):
                session.add(
                    AuditLog(action="delete_document", detail=f"id={doc.id} name={doc.file_name} batch=1")
                )
                session.delete(doc)  # 字段/校验结果随级联删除
                deleted += 1
            session.commit()

        self._select_all_btn.setChecked(False)  # 重置表头全选，避免刷新后勾选态残留
        self.reload()
        self._toast.show_message(f"已删除 {deleted} 条记录")

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
        self._action_table.removeRow(row)
        self._update_select_count()
        self._toast.show_message("已删除")
