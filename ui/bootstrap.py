"""启动引导：结构迁移 + 日志 + 异常兜底。

打包成 windowed exe 后没有控制台，一旦启动期抛异常，用户只会看到"双击没反应"
或"打开失败"。所以启动链路统一走这里：

  · setup_logging()      日志写到 %APPDATA%\\PdfDataTool\\logs\\app.log
  · install_excepthook() 未捕获异常写日志（而不是静默崩溃）
  · bootstrap_database() 升级老库结构；失败时弹出人话提示 + 数据库路径

数据库路径/备份/损坏处理都在 :mod:`database.migrations`，这里只负责
"让用户看懂发生了什么、下一步该做什么"。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from database.db import ensure_schema
from database.migrations import MigrationReport, SchemaError, SchemaTooNewError
from paths import log_file_path, log_dir

_LOG_CONFIGURED = False
_HOOK_INSTALLED = False


def setup_logging(level: int = logging.INFO) -> Path | None:
    """把日志落到数据目录（失败不影响运行：日志本身不该阻断启动）。"""
    global _LOG_CONFIGURED
    target: Path | None = None
    try:
        log_dir().mkdir(parents=True, exist_ok=True)
        target = log_file_path()
    except OSError:
        target = None

    if _LOG_CONFIGURED:
        return target

    handlers: list[logging.Handler] = []
    if target is not None:
        try:
            handlers.append(logging.FileHandler(target, encoding="utf-8"))
        except OSError:
            handlers = []
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=handlers,
        force=True,
    )
    _LOG_CONFIGURED = True
    return target


def install_excepthook() -> None:
    """未捕获异常进日志（windowed 模式下没有控制台可看 traceback）。"""
    global _HOOK_INSTALLED
    if _HOOK_INSTALLED:
        return
    previous = sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        logging.getLogger("app").critical(
            "未捕获异常", exc_info=(exc_type, exc_value, exc_tb)
        )
        if previous is not None:
            previous(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook
    _HOOK_INSTALLED = True


def bootstrap_database(db_path: str, parent=None) -> MigrationReport | None:
    """启动前对齐数据库结构；成功返回报告，失败提示用户并返回 None。

    能自愈的情况（列/表缺失、库文件损坏）在 ``ensure_schema`` 内部已处理完，
    这里只处理"真的打不开"：结构版本比程序新、库被占用/无权限等。
    """
    logger = logging.getLogger("startup")
    try:
        report = ensure_schema(db_path)
    except SchemaTooNewError as exc:
        _report_failure(
            "数据库版本过新",
            f"{exc}\n\n数据库位置：\n{db_path}\n\n"
            "处理建议：使用与数据库匹配的新版本程序，或先把该文件改名备份，"
            "让程序重新建库。",
            parent,
        )
        return None
    except SchemaError as exc:
        _report_failure(
            "数据库无法打开",
            f"{exc}\n\n数据库位置：\n{db_path}\n\n"
            "处理建议：确认文件未被其他程序占用、所在目录可写；"
            "必要时把该文件改名备份后重试（程序会自动新建空库）。",
            parent,
        )
        return None
    except Exception as exc:  # noqa: BLE001 - 启动兜底：任何异常都要给用户提示
        logger.exception("数据库初始化失败")
        _report_failure(
            "数据库初始化失败",
            f"{type(exc).__name__}: {exc}\n\n数据库位置：\n{db_path}\n\n"
            f"详细信息见日志：{log_file_path()}",
            parent,
        )
        return None

    if report.migrated:
        logger.info("数据库结构已对齐：%s（%s）", report.summary(), db_path)
    else:
        logger.debug("数据库结构无需变更：%s", db_path)
    return report


def _report_failure(title: str, detail: str, parent) -> None:
    logging.getLogger("startup").error("%s：%s", title, detail.replace("\n", " "))
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError:  # CLI/无 Qt 环境
        print(f"[{title}] {detail}", file=sys.stderr)
        return
    if QApplication.instance() is None:
        print(f"[{title}] {detail}", file=sys.stderr)
        return
    QMessageBox.critical(parent, title, detail)
