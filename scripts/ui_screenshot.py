"""离屏截图：管理后台三个页面 + 详情弹窗。

运行：QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -X utf8 scripts/ui_screenshot.py
输出：docs/gui_screenshot_admin.png / _filter.png / _settings.png / _detail.png
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QTimer

from ui.app import create_app
from ui.main_window import MainWindow


def grab(widget, path: str) -> None:
    pix = widget.grab()
    pix.save(path)
    print(f"saved {path}", flush=True)


def main() -> int:
    docs = Path("docs")
    docs.mkdir(exist_ok=True)

    app = create_app()
    win = MainWindow()
    win.resize(1280, 820)
    win.show()

    # 上传页（默认）
    QTimer.singleShot(300, lambda: (
        grab(win, str(docs / "gui_screenshot_admin.png")),
        win._switch_page("filter"),
    ))

    def shot_filter() -> None:
        grab(win, str(docs / "gui_screenshot_filter.png"))
        win._switch_page("settings")
        QTimer.singleShot(200, shot_settings)

    def shot_settings() -> None:
        grab(win, str(docs / "gui_screenshot_settings.png"))
        shot_detail()

    def shot_detail() -> None:
        from ui.pages.detail_dialog import DetailDialog

        dlg = DetailDialog("data/app.db", 1, "contract_sample.pdf", win)
        dlg.resize(980, 640)
        dlg.show()

        def snap() -> None:
            grab(dlg, str(docs / "gui_screenshot_detail.png"))
            dlg.close()
            win._upload_page.shutdown()
            app.exit(0)

        QTimer.singleShot(400, snap)

    QTimer.singleShot(500, shot_filter)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
