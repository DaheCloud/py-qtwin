"""应用入口：创建 QApplication 并显示主窗口。"""

from __future__ import annotations

from PySide6.QtWidgets import QApplication

from paths import default_db_path, ensure_data_dir
from ui.bootstrap import bootstrap_database, install_excepthook, setup_logging
from ui.main_window import MainWindow
from ui.pages.settings_page import get_theme_choice, load_settings
from ui.styles import apply_theme, make_app_icon, ui_font


def create_app() -> QApplication:
    """创建（或复用）QApplication，并按持久化设置应用主题。"""
    setup_logging()
    install_excepthook()
    app = QApplication.instance() or QApplication([])
    apply_theme(app, get_theme_choice(load_settings()))
    app.setWindowIcon(make_app_icon())
    app.setFont(ui_font(10))
    return app


def run_app(db_path: str | None = None) -> int:
    ensure_data_dir()  # 幂等：建目录 + 旧数据一次性迁移
    app = create_app()
    # 老库结构自动升级；打不开时给出人话提示并退出（而不是空列表假成功）
    if bootstrap_database(db_path or default_db_path()) is None:
        return 3
    window = MainWindow(db_path=db_path or default_db_path())
    window.show()
    code = app.exec()
    # exec() 返回后窗口对象仍由 Python 持有；必须先停掉后台解析线程，
    # 否则解释器退出时 QThread 仍在运行会导致硬崩溃（Windows exit 127）。
    window.shutdown()
    return code


if __name__ == "__main__":
    raise SystemExit(run_app())
