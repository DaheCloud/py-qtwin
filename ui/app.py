"""应用入口：创建 QApplication 并显示主窗口。"""

from __future__ import annotations

from PySide6.QtWidgets import QApplication

from paths import default_db_path
from ui.main_window import MainWindow
from ui.pages.settings_page import get_theme_choice, load_settings
from ui.styles import apply_theme, make_app_icon, ui_font


def create_app() -> QApplication:
    """创建（或复用）QApplication，并按持久化设置应用主题。"""
    app = QApplication.instance() or QApplication([])
    apply_theme(app, get_theme_choice(load_settings()))
    app.setWindowIcon(make_app_icon())
    app.setFont(ui_font(10))
    return app


def run_app(db_path: str | None = None) -> int:
    app = create_app()
    window = MainWindow(db_path=db_path or default_db_path())
    window.show()
    code = app.exec()
    # exec() 返回后窗口对象仍由 Python 持有；必须先停掉后台解析线程，
    # 否则解释器退出时 QThread 仍在运行会导致硬崩溃（Windows exit 127）。
    window.shutdown()
    return code


if __name__ == "__main__":
    raise SystemExit(run_app())
