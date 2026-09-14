"""筛选页测试：开票年月过滤（纯函数）+ 一键确认（离屏 UI 流程）。

年月解析口径：
  · 各种日期写法（标准/中文/斜杠）→ "YYYY-MM"
  · 旧合同数据无 invoice_date 时回落 sign_date
  · 不可解析 → 空串（不参与下拉选项）

一键确认：勾选可疑记录 → 批量标记为「准确」；非可疑状态跳过、取消不生效。

运行：.venv/Scripts/python.exe -m pytest tests/test_filter_page.py -v
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui.pages.filter_page import COL_CHECK, COL_NAME, record_month, year_month


def test_year_month_parses_common_formats():
    assert year_month("2026-09-09") == "2026-09"
    assert year_month("2026-9-9") == "2026-09"
    assert year_month("2026年09月09日") == "2026-09"
    assert year_month("2026年9月9日") == "2026-09"
    assert year_month("2026/09/09") == "2026-09"
    assert year_month("2026.09.09") == "2026-09"


def test_year_month_rejects_unparsable():
    assert year_month(None) == ""
    assert year_month("") == ""
    assert year_month("无日期") == ""
    assert year_month("HT20260901") == ""  # 合同编号这类不该被当成日期


def test_record_month_prefers_invoice_date_then_sign_date():
    assert record_month({"invoice_date": "2026-09-09"}) == "2026-09"
    assert record_month({"invoice_date": None, "sign_date": "2025-12-31"}) == "2025-12"
    assert record_month({"contract_no": "HT20260901"}) == ""


# --------------------------------------------------------------- 一键确认（UI 流程）


def _select_all(page, checked: bool = True) -> None:
    for row in range(page._table.rowCount()):
        page._table.cellWidget(row, COL_CHECK).setChecked(checked)


def _row_of(page, file_name: str) -> int:
    """按文件名定位行（列表按导入时间倒序，不能假定固定行号）。"""
    for row in range(page._table.rowCount()):
        if page._table.item(row, COL_NAME).text() == file_name:
            return row
    raise AssertionError(f"列表中找不到 {file_name}")


def _statuses(db_path) -> dict[str, str]:
    from sqlalchemy import select

    from database.db import get_engine, make_session_factory
    from models.document import Document

    with make_session_factory(get_engine(db_path))() as session:
        return {doc.file_name: doc.status for doc in session.scalars(select(Document))}


# 演示数据：可疑 2 条（manual_review / warning）、准确 1、失败 1
CONFIRM_DOCS = (
    ("review.pdf", "manual_review"),
    ("warning.pdf", "warning"),
    ("success.pdf", "success"),
    ("failed.pdf", "failed"),
)


@pytest.fixture
def qt_app():
    """离屏 QApplication（platform 由 conftest 统一设为 offscreen）。"""
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _make_page(db_path, docs=()) -> object:
    """建库（可选写入演示记录）并构造筛选页。"""
    from database.db import get_engine, init_db, make_session_factory
    from models.document import Document
    from ui.pages.filter_page import FilterPage

    engine = get_engine(db_path)
    init_db(engine)
    if docs:
        with make_session_factory(engine)() as session:
            for name, status in docs:
                session.add(
                    Document(
                        file_name=name,
                        file_path=f"C:/{name}",
                        file_hash=f"hash-{name}",
                        status=status,
                        error_reason=None if status == "success" else "需人工核对",
                    )
                )
            session.commit()
    return FilterPage(str(db_path))


@pytest.fixture
def confirm_page(qt_app, tmp_path):
    """带 4 条记录的筛选页（见 CONFIRM_DOCS）。"""
    db_path = tmp_path / "app.db"
    return _make_page(db_path, CONFIRM_DOCS), db_path


@pytest.fixture
def empty_page(qt_app, tmp_path):
    """空库筛选页：用于验证空列表下的文案/按钮表现。"""
    db_path = tmp_path / "empty.db"
    return _make_page(db_path), db_path


def _answer_yes(monkeypatch, yes: bool = True):
    from PySide6.QtWidgets import QMessageBox

    button = QMessageBox.StandardButton.Yes if yes else QMessageBox.StandardButton.No
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: button))


def test_confirm_button_present(confirm_page):
    page, _ = confirm_page
    assert "一键确认" in page._confirm_btn.text()
    assert "可疑" in page._confirm_btn.toolTip()


def test_one_click_confirm_marks_selected_suspect(confirm_page, monkeypatch):
    page, db_path = confirm_page
    _answer_yes(monkeypatch)
    _select_all(page)

    page._confirm_selected()

    assert _statuses(db_path) == {
        "review.pdf": "success",
        "warning.pdf": "success",
        "success.pdf": "success",
        "failed.pdf": "failed",  # 解析失败不参与一键确认
    }
    assert "已确认 2 条" in page._toast.text()
    # 确认后重置表头全选，避免刷新后勾选态残留在新行上
    assert page._select_all_btn.isChecked() is False


def test_confirm_skips_when_selection_has_no_suspect(confirm_page, monkeypatch):
    page, db_path = confirm_page
    _answer_yes(monkeypatch)
    # 仅勾选「准确」那条（行序按导入时间倒序，按文件名定位）
    page._table.cellWidget(_row_of(page, "success.pdf"), COL_CHECK).setChecked(True)

    page._confirm_selected()

    assert _statuses(db_path)["success.pdf"] == "success"
    assert "没有" in page._toast.text()
    # 未确认时不写审计日志
    from sqlalchemy import select

    from database.db import get_engine, make_session_factory
    from models.document import AuditLog

    with make_session_factory(get_engine(db_path))() as session:
        assert session.scalars(select(AuditLog)).all() == []


def test_confirm_cancel_keeps_status(confirm_page, monkeypatch):
    page, db_path = confirm_page
    _answer_yes(monkeypatch, yes=False)
    _select_all(page)

    page._confirm_selected()

    assert _statuses(db_path)["review.pdf"] == "manual_review"


def test_confirm_clears_review_filter_after_reload(confirm_page, monkeypatch):
    """在「可疑/待校验」筛选下确认后，这些行应从列表消失（状态已变准确）。"""
    page, _ = confirm_page
    page._status_filter.setCurrentIndex(page._status_filter.findData("review"))
    page.reload()
    assert page._table.rowCount() == 2

    _answer_yes(monkeypatch)
    _select_all(page)
    page._confirm_selected()

    assert page._table.rowCount() == 0


# ------------------------------------------------------- "已选择 N 项" 计数（UI 流程）


def _count_text(page) -> str:
    """去掉 <b> 标签后的计数文案，例如 "已选择 3 项"。"""
    return re.sub(r"<[^>]+>", "", page._select_count_label.text())


def test_select_count_starts_at_zero(confirm_page):
    page, _ = confirm_page
    assert _count_text(page) == "已选择 0 项"
    assert page._select_count_label.isHidden() is False  # 有数据时显示


def test_select_count_updates_on_row_check(confirm_page):
    """手动勾选/取消勾选要实时刷新（修复前只在 reload 后刷新，会一直停在 0）。"""
    page, _ = confirm_page
    check = page._table.cellWidget(_row_of(page, "warning.pdf"), COL_CHECK)

    check.setChecked(True)
    assert _count_text(page) == "已选择 1 项"

    page._table.cellWidget(_row_of(page, "review.pdf"), COL_CHECK).setChecked(True)
    assert _count_text(page) == "已选择 2 项"

    check.setChecked(False)
    assert _count_text(page) == "已选择 1 项"


def test_select_count_updates_on_select_all(confirm_page):
    page, _ = confirm_page

    page._select_all_btn.setChecked(True)
    assert _count_text(page) == "已选择 4 项"

    page._select_all_btn.setChecked(False)
    assert _count_text(page) == "已选择 0 项"


def test_select_count_refreshes_after_data_reload(confirm_page):
    """刷新数据后勾选态清空、计数归零（状态筛选切换走到 reload）。"""
    page, _ = confirm_page
    page._select_all_btn.setChecked(True)
    assert _count_text(page) == "已选择 4 项"

    page._status_filter.setCurrentIndex(page._status_filter.findData("review"))
    page.reload()

    assert page._table.rowCount() == 2
    assert _count_text(page) == "已选择 0 项"


def test_select_count_label_hidden_when_list_empty(empty_page):
    """空列表不显示"已选择 0 项"（无意义文案）。"""
    page, _ = empty_page
    assert page._table.rowCount() == 0
    assert page._select_count_label.isHidden() is True

