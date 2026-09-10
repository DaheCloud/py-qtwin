"""运行路径基准：兼容脚本运行与 PyInstaller 打包后的 exe 运行。

- base_dir()：只读资源根（templates/ 等随包分发）。
  打包后位于 PyInstaller 解包目录（onedir 模式为 <exe>/_internal）。
- data_dir()：可写数据根（SQLite 数据库等）。
  打包后位于 %APPDATA%\\PdfDataTool\\，与程序目录彻底解耦——
  升级 / 整包替换 / 重装都不会触碰用户数据；脚本开发模式仍用项目根 data/。
- ensure_data_dir()：创建数据目录，并在打包模式下把旧版遗留的
  <exe>/data/（exe 同目录）一次性迁移到 %APPDATA%，旧目录原样保留作备份。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

_APP_DIR_NAME = "PdfDataTool"
_DB_FILENAME = "app.db"


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def base_dir() -> Path:
    if _is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
    return Path(__file__).resolve().parent


def _appdata_root() -> Path:
    """Windows %APPDATA%（Roaming）；环境变量缺失时回退到用户主目录。"""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata)
    return Path.home() / "AppData" / "Roaming"


def data_dir() -> Path:
    if _is_frozen():
        return _appdata_root() / _APP_DIR_NAME
    return Path(__file__).resolve().parent / "data"


def ensure_data_dir() -> Path:
    """确保数据目录存在；打包模式下执行一次性旧数据迁移（幂等）。

    旧版把 app.db 放在 exe 同目录 data/ 下。检测到旧库存在而新库
    尚不存在时，将整个旧 data 目录拷贝到 %APPDATA%\\PdfDataTool\\；
    旧目录保留不动（相当于备份）。任何迁移异常都不应阻断启动。
    """
    target = data_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
        if _is_frozen():
            legacy = Path(sys.executable).resolve().parent / "data"
            legacy_db = legacy / _DB_FILENAME
            new_db = target / _DB_FILENAME
            if legacy_db.exists() and not new_db.exists():
                shutil.copytree(legacy, target, dirs_exist_ok=True)
    except OSError:
        pass
    return target


def templates_dir() -> Path:
    return base_dir() / "templates"


def default_db_path() -> str:
    """默认 SQLite 路径；打包后位于 %APPDATA%\\PdfDataTool\\app.db。"""
    return str(data_dir() / _DB_FILENAME)
