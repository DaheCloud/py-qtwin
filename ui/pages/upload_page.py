"""页面 1：文件上传与管理（对应 ui.html 的 #page-upload）。

拖拽/点选 PDF → 入队动画（模拟上传进度）→ 后台线程真实解析落库，
状态徽章跟随流转：上传中… → 解析中… → 已完成 / 解析失败 / 待人工确认。
"""

from __future__ import annotations

import atexit
import queue
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ui.styles import ui_font
from ui.widgets.common import BadgeDelegate, ProgressDelegate, Toast

_STATUS_TO_KIND = {
    "success": "success",
    "manual_review": "warning",
    "warning": "warning",
    "failed": "failed",
    "processing": "info",
    "pending": "info",
}

_ROW_ID = Qt.ItemDataRole.UserRole + 1
_ROW_PATH = Qt.ItemDataRole.UserRole + 2
_ROW_STATE = Qt.ItemDataRole.UserRole + 3  # uploading / parsing / done / failed
_ROW_FORCE = Qt.ItemDataRole.UserRole + 4  # 覆盖导入（手动确认过查重）

COL_NAME, COL_SIZE, COL_TIME, COL_PROGRESS, COL_STATUS, COL_ACTION = range(6)


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class _ParseWorker(QObject):
    """后台解析线程：串行消费解析队列，避免并发写 SQLite。"""

    task_done = Signal(str, str, str, int)  # row_id, status, reason, document_id

    def __init__(self, db_path: str) -> None:
        super().__init__()
        self._db_path = db_path
        self._tasks: queue.Queue[tuple[str, str, bool] | None] = queue.Queue()

    def submit(self, row_id: str, pdf_path: str, force: bool = False) -> None:
        self._tasks.put((row_id, pdf_path, force))

    def stop(self) -> None:
        self._tasks.put(None)

    def run(self) -> None:  # type: ignore[override]
        from database.db import get_engine, init_db, make_session_factory
        from paths import templates_dir
        from pdf.template_engine import TemplateEngine
        from services.pdf_service import PdfService

        engine = get_engine(self._db_path)
        init_db(engine)
        factory = make_session_factory(engine)
        service = PdfService(TemplateEngine(templates_dir()))

        while True:
            task = self._tasks.get()
            if task is None:
                return
            row_id, pdf_path, force = task
            try:
                with factory() as session:
                    doc = service.process_document(session, pdf_path, None, force=force)
                    reason = doc.error_reason or ("解析完成" if doc.status == "success" else "")
                    self.task_done.emit(row_id, doc.status, reason, int(doc.id))
            except Exception as exc:  # noqa: BLE001 — 后台线程兜底，错误回填到行
                self.task_done.emit(row_id, "failed", str(exc), 0)


class _DropZone(QFrame):
    """拖拽上传区（对应 .drop-zone）。"""

    files_dropped = Signal(list)
    pick_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(110)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(12)
        hint = QLabel("拖拽多个 PDF 文件至此处，或点击选择文件上传")
        hint.setObjectName("DropZoneHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)
        btn = QPushButton("选取本地文件")
        btn.setProperty("cssClass", "btn-primary")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(self.pick_requested.emit)
        wrap = QHBoxLayout()
        wrap.addStretch(1)
        wrap.addWidget(btn)
        wrap.addStretch(1)
        layout.addLayout(wrap)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setProperty("dragOver", True)
            self.style().unpolish(self)
            self.style().polish(self)

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self.setProperty("dragOver", False)
        self.style().unpolish(self)
        self.style().polish(self)

    def dropEvent(self, event) -> None:  # noqa: N802
        self.setProperty("dragOver", False)
        self.style().unpolish(self)
        self.style().polish(self)
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.toLocalFile().lower().endswith(".pdf")]
        if paths:
            self.files_dropped.emit(paths)


class UploadPage(QWidget):
    """上传 + 队列 + 解析状态页。"""

    database_changed = Signal()  # 解析落库后通知筛选页刷新

    def __init__(self, db_path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._db_path = db_path
        self._toast = Toast(self)
        self._error_toast = Toast(self)  # 错误专用：5s，独立于普通提示，不被其覆盖

        # 批量结果聚合：600ms 无新完成事件后汇总一条提示，
        # 避免多文件批量时提示互相覆盖（每个失败原因仍在行悬停提示中）
        self._pending_success: list[str] = []
        self._pending_error: list[tuple[str, str]] = []  # (name, reason)
        self._batch_timer = QTimer(self)
        self._batch_timer.setSingleShot(True)
        self._batch_timer.setInterval(600)
        self._batch_timer.timeout.connect(self._flush_batch_summary)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)

        self._drop = _DropZone()
        self._drop.files_dropped.connect(self.handle_paths)
        self._drop.pick_requested.connect(self._pick_files)
        root.addWidget(self._drop)

        card = QFrame()
        card.setObjectName("Card")
        card_l = QVBoxLayout(card)
        card_l.setContentsMargins(16, 16, 16, 16)
        card_l.setSpacing(12)

        head = QHBoxLayout()
        title = QLabel("上传队列与解析状态")
        title.setObjectName("CardTitle")
        head.addWidget(title)
        head.addStretch(1)
        clear_btn = QPushButton("清空已完成")
        clear_btn.setProperty("cssClass", "btn-default")
        clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_btn.clicked.connect(self._clear_completed)
        head.addWidget(clear_btn)
        card_l.addLayout(head)

        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(["文件名", "文件大小", "导入时间", "上传进度", "状态", "操作"])
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(48)  # 容纳按钮文字（YaHei 行高较高）
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self._table.horizontalHeader()
        header.setMinimumSectionSize(56)
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        # Interactive：默认宽度合理，且用户可拖动列边界自行调整
        for col, width in ((COL_SIZE, 80), (COL_TIME, 175), (COL_PROGRESS, 170), (COL_STATUS, 100), (COL_ACTION, 90)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            self._table.setColumnWidth(col, width)
        self._table.setItemDelegateForColumn(COL_PROGRESS, ProgressDelegate(self._table))
        self._table.setItemDelegateForColumn(COL_STATUS, BadgeDelegate(self._table))
        card_l.addWidget(self._table, 1)
        root.addWidget(card, 1)

        # 模拟上传进度定时器（对应 JS simulateUpload 的 300ms 步进）
        self._timer = QTimer(self)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self._tick_progress)

        self._worker_thread: QThread | None = None
        self._worker: _ParseWorker | None = None
        self._start_worker()
        # 进程退出时兜底停线程，否则 QThread 仍在运行会导致硬崩溃（exit 127）
        self._exit_hook = atexit.register(self.shutdown)

    # ------------------------------------------------------------- 对外

    def handle_paths(self, paths: list[str]) -> None:
        if not paths:
            return
        normal, force_paths = self._split_duplicates(paths)
        import_time = _now_str()  # 与 JS 一致：本次添加动作统一时间
        force_set = set(force_paths)
        for path in normal + force_paths:
            row_id = f"{datetime.now().timestamp():.6f}-{id(path)}"
            self._add_row(row_id, path, import_time, force=path in force_set)
        msg = f"已成功添加 {len(normal) + len(force_paths)} 个文件到队列"
        if force_paths:
            msg += f"（其中 {len(force_paths)} 个为覆盖导入）"
        self._toast.show_message(msg)
        if not self._timer.isActive():
            self._timer.start()

    def _split_duplicates(self, paths: list[str]) -> tuple[list[str], list[str]]:
        """入库前主线程预检重复。

        返回 (普通路径, 用户确认覆盖的路径)；用户拒绝覆盖的重复文件被剔除。
        """
        from database.db import get_engine, init_db, make_session_factory
        from models.document import Document
        from services.pdf_service import file_sha256

        engine = get_engine(self._db_path)
        init_db(engine)
        factory = make_session_factory(engine)
        normal, force_paths = [], []
        with factory() as session:
            for p in paths:
                try:
                    h = file_sha256(p)
                except OSError:
                    normal.append(p)  # 文件读不了，交由解析阶段报错
                    continue
                if session.query(Document).filter_by(file_hash=h).one_or_none() is not None:
                    force_paths.append(p)
                else:
                    normal.append(p)
        if force_paths:
            names = "\n".join(f"· {Path(p).name}" for p in force_paths)
            answer = QMessageBox.question(
                self,
                "发现重复文件",
                f"以下 {len(force_paths)} 个文件已导入过：\n{names}\n\n"
                "是否删除旧记录并重新导入？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return normal, []
        return normal, force_paths

    # ------------------------------------------------------------- 私有

    def _pick_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "选取 PDF 文件", "", "PDF 文件 (*.pdf)")
        self.handle_paths(paths)

    def _add_row(self, row_id: str, path: str, import_time: str, force: bool = False) -> None:
        r = self._table.rowCount()
        self._table.insertRow(r)

        name_item = QTableWidgetItem(Path(path).name)
        name_item.setData(_ROW_ID, row_id)
        name_item.setData(_ROW_PATH, path)
        name_item.setData(_ROW_STATE, "uploading")
        name_item.setData(_ROW_FORCE, force)
        name_item.setToolTip(path)
        self._table.setItem(r, COL_NAME, name_item)

        try:
            size_mb = Path(path).stat().st_size / 1024 / 1024
            size_text = f"{size_mb:.2f} MB"
        except OSError:
            size_text = "—"
        self._table.setItem(r, COL_SIZE, QTableWidgetItem(size_text))

        time_item = QTableWidgetItem(import_time)
        time_item.setForeground(Qt.GlobalColor.gray)
        time_item.setToolTip(import_time)  # 列被拖窄时悬停仍可看完整时间
        self._table.setItem(r, COL_TIME, time_item)

        self._table.setItem(r, COL_PROGRESS, QTableWidgetItem())
        self._table.item(r, COL_PROGRESS).setData(Qt.ItemDataRole.UserRole, 0)
        self._table.setItem(r, COL_STATUS, QTableWidgetItem())
        self._set_status(r, "info", "上传中...")

        cancel = QPushButton("取消")
        cancel.setProperty("cssClass", "btn-danger-text")
        cancel.setFixedHeight(26)
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(lambda _=False, rid=row_id: self._remove_row(rid))
        self._table.setCellWidget(r, COL_ACTION, cancel)
        self._center_cell_widget(r, COL_ACTION)

    def _center_cell_widget(self, row: int, col: int) -> None:
        """让行内 cell widget 在单元格中垂直居中。"""
        w = self._table.cellWidget(row, col)
        if w is None:
            return
        h = self._table.rowHeight(row)
        w.move(w.x(), max(0, (h - w.height()) // 2))

    def _set_status(self, row: int, kind: str, text: str) -> None:
        item = self._table.item(row, COL_STATUS)
        item.setText(text)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        item.setData(Qt.ItemDataRole.UserRole, kind)

    def _tick_progress(self) -> None:
        import random

        active = False
        for r in range(self._table.rowCount()):
            name_item = self._table.item(r, COL_NAME)
            if name_item.data(_ROW_STATE) != "uploading":
                continue
            active = True
            row_id = name_item.data(_ROW_ID)
            progress = self._progress_of(r)
            progress += random.randint(10, 35)
            if progress >= 100:
                self._table.item(r, COL_PROGRESS).setData(Qt.ItemDataRole.UserRole, 100)
                name_item.setData(_ROW_STATE, "parsing")
                self._set_status(r, "info", "解析中...")
                self._submit_parse(row_id, name_item.data(_ROW_PATH), bool(name_item.data(_ROW_FORCE)))
            else:
                self._table.item(r, COL_PROGRESS).setData(Qt.ItemDataRole.UserRole, progress)
        if not active:
            self._timer.stop()

    def _progress_of(self, row: int) -> int:
        value = self._table.item(row, COL_PROGRESS).data(Qt.ItemDataRole.UserRole)
        return int(value or 0)

    def _submit_parse(self, row_id: str, path: str, force: bool = False) -> None:
        if self._worker is not None:
            self._worker.submit(row_id, path, force)

    def _on_parse_done(self, row_id: str, status: str, reason: str, document_id: int) -> None:
        row = self._find_row(row_id)
        if row is None:  # 行已被取消
            return
        kind = _STATUS_TO_KIND.get(status, "warning")
        text = {
            "success": "已完成",
            "manual_review": "待人工确认",
            "warning": "待人工确认",
            "failed": "解析失败",
        }.get(status, status)
        self._table.item(row, COL_NAME).setData(_ROW_STATE, "done" if kind == "success" else "failed")
        self._set_status(row, kind, text)
        name = Path(self._table.item(row, COL_NAME).data(_ROW_PATH)).name
        tip = f"{name} · {text}：{reason}" if reason else f"{name} · {text}"
        self._table.item(row, COL_STATUS).setToolTip(tip)
        if kind == "success":
            self._pending_success.append(name)
        else:
            self._pending_error.append((name, reason or text))
        self._batch_timer.start()  # 重新计时；600ms 静默后统一汇总
        self.database_changed.emit()

    def _flush_batch_summary(self) -> None:
        """把窗口期内积累的完成事件汇总成一条（或多条）提示。"""
        successes = self._pending_success
        errors = self._pending_error
        self._pending_success = []
        self._pending_error = []

        if not successes and not errors:
            return

        # 成功：单个→单条；多个→汇总一条
        if len(successes) == 1 and not errors:
            self._toast.show_message(f"{successes[0]}：已完成")
        elif successes:
            self._toast.show_message(f"批量解析完成：成功 {len(successes)} 个")

        # 失败/待人工确认：单个→完整原因 5s；多个→汇总 5s（原因悬停行状态列可见）
        if len(errors) == 1 and not successes:
            name, reason = errors[0]
            self._error_toast.show_message(f"{name}：解析失败 — {reason}", msec=5000)
        elif errors:
            failed = [(n, r) for n, r in errors if "待人工确认" not in r]
            review = [(n, r) for n, r in errors if "待人工确认" in r]
            parts = []
            if failed:
                parts.append(f"解析失败 {len(failed)} 个")
            if review:
                parts.append(f"待人工确认 {len(review)} 个")
            names = "、".join(n for n, _ in errors[:3])
            more = "等" if len(errors) > 3 else ""
            self._error_toast.show_message(
                f"批量结果：{'，'.join(parts)}（{names}{more}）— 悬停行状态列查看原因",
                msec=5000,
            )

    def _find_row(self, row_id: str) -> int | None:
        for r in range(self._table.rowCount()):
            if self._table.item(r, COL_NAME).data(_ROW_ID) == row_id:
                return r
        return None

    def _remove_row(self, row_id: str) -> None:
        row = self._find_row(row_id)
        if row is None:
            return
        state = self._table.item(row, COL_NAME).data(_ROW_STATE)
        if state == "parsing":
            self._toast.show_message("该文件正在解析，无法取消")
            return
        self._table.removeRow(row)
        if self._table.rowCount() == 0:
            self._timer.stop()

    def _clear_completed(self) -> None:
        removed = 0
        for r in range(self._table.rowCount() - 1, -1, -1):
            state = self._table.item(r, COL_NAME).data(_ROW_STATE)
            if state == "done":
                self._table.removeRow(r)
                removed += 1
        self._toast.show_message("已清理完成项" if removed else "没有可清理的完成项")

    def _start_worker(self) -> None:
        self._worker_thread = QThread(self)
        self._worker = _ParseWorker(self._db_path)
        self._worker.moveToThread(self._worker_thread)
        self._worker.task_done.connect(self._on_parse_done)
        self._worker_thread.started.connect(self._worker.run)
        self._worker_thread.start()

    def shutdown(self) -> None:
        if self._worker is None:
            return
        self._worker.stop()
        if self._worker_thread is not None:
            self._worker_thread.quit()
            self._worker_thread.wait(3000)
        self._worker = None
        self._worker_thread = None
        if self._exit_hook is not None:
            atexit.unregister(self._exit_hook)
            self._exit_hook = None
