"""UI 冒烟验证：外壳、四个页面、详情弹窗均可创建并正常联动。

运行：QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -X utf8 scripts/ui_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def p(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    from PySide6.QtCore import Qt

    from ui.app import create_app
    from ui.main_window import MainWindow

    p("1: create_app")
    app = create_app()

    p("2: MainWindow construct")
    win = MainWindow()

    p("3: show")
    win.show()

    assert win._stack.count() == 4, "应有 4 个页面"

    p("4: sidebar order")
    lst = win._sidebar._list
    keys = [lst.item(i).data(Qt.ItemDataRole.UserRole) for i in range(lst.count())]
    assert keys == ["upload", "filter", "settings", "editor"], f"侧边栏顺序异常：{keys}"

    p("5: page switching")
    for key, page in (
        ("upload", win._upload_page),
        ("filter", win._filter_page),
        ("settings", win._settings_page),
        ("editor", win._editor_page),
    ):
        win._switch_page(key)
        assert win._stack.currentWidget() is page, f"{key} 未切到对应页面"
    win._switch_page("upload")
    assert win._stack.currentWidget() is win._upload_page

    p("6: settings save/load")
    from ui.pages.settings_page import get_confidence_threshold

    original = get_confidence_threshold()
    win._settings_page._threshold.setValue(90)
    win._settings_page._save()
    assert get_confidence_threshold() == 90
    # 恢复运行前的原值，避免冒烟脚本改掉用户设置
    win._settings_page._threshold.setValue(original)
    win._settings_page._save()
    assert get_confidence_threshold() == original

    p("7: detail dialog (missing id)")
    from ui.pages.detail_dialog import DetailDialog

    dlg = DetailDialog("data/app.db", 999999, "nonexistent.pdf", win)
    dlg.close()

    p("8: shutdown worker (mirrors run_app post-exec)")
    win._upload_page.shutdown()
    del win  # 模拟 exec() 返回后窗口被回收：线程必须已停止

    p("UI smoke OK: shell + 4 pages + settings + detail dialog")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
