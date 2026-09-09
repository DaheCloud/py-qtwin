"""UI 冒烟验证：外壳、三个页面、详情弹窗均可创建并正常联动。

运行：QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -X utf8 scripts/ui_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def p(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    from ui.app import create_app
    from ui.main_window import MainWindow

    p("1: create_app")
    app = create_app()

    p("2: MainWindow construct")
    win = MainWindow()

    p("3: show")
    win.show()

    assert win._stack.count() == 3, "应有 3 个页面"

    p("4: page switching")
    win._switch_page("filter")
    assert win._stack.currentIndex() == 1

    win._switch_page("settings")
    assert win._stack.currentIndex() == 2

    win._switch_page("upload")
    assert win._stack.currentIndex() == 0

    p("5: settings save/load")
    win._settings_page._threshold.setValue(90)
    win._settings_page._save()
    from ui.pages.settings_page import get_confidence_threshold

    assert get_confidence_threshold() == 90
    win._settings_page._threshold.setValue(85)
    win._settings_page._save()

    p("6: detail dialog (missing id)")
    from ui.pages.detail_dialog import DetailDialog

    dlg = DetailDialog("data/app.db", 999999, "nonexistent.pdf", win)
    dlg.close()

    p("7: shutdown worker (mirrors run_app post-exec)")
    win._upload_page.shutdown()
    del win  # 模拟 exec() 返回后窗口被回收：线程必须已停止

    p("UI smoke OK: shell + 3 pages + settings + detail dialog")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
