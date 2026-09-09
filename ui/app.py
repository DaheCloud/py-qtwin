"""应用入口：创建 QApplication 并显示主窗口。"""

from __future__ import annotations

from PySide6.QtWidgets import QApplication

from ui.main_window import MainWindow
from ui.styles import STYLE_SHEET, make_app_icon, ui_font


def create_app() -> QApplication:
    """创建（或复用）QApplication，并应用全局主题。"""
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(STYLE_SHEET)
    app.setWindowIcon(make_app_icon())
    app.setFont(ui_font(10))
    return app


def run_app(db_path: str = "data/app.db") -> int:
    app = create_app()
    window = MainWindow(db_path=db_path)
    window.show()
    code = app.exec()
    # exec() 返回后窗口对象仍由 Python 持有；必须先停掉后台解析线程，
    # 否则解释器退出时 QThread 仍在运行会导致硬崩溃（Windows exit 127）。
    window.shutdown()
    return code


if __name__ == "__main__":
    raise SystemExit(run_app())
