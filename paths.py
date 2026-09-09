"""运行路径基准：兼容脚本运行与 PyInstaller 打包后的 exe 运行。

- base_dir()：只读资源根（templates/ 等随包分发）。
  打包后位于 PyInstaller 解包目录（onedir 模式为 <exe>/_internal）。
- data_dir()：可写数据根（SQLite 数据库等）。
  打包后位于 exe 同目录的 data/ 下，便于用户备份与迁移。
"""

from __future__ import annotations

import sys
from pathlib import Path


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def base_dir() -> Path:
    if _is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    if _is_frozen():
        return Path(sys.executable).resolve().parent / "data"
    return Path(__file__).resolve().parent / "data"


def templates_dir() -> Path:
    return base_dir() / "templates"


def default_db_path() -> str:
    """默认 SQLite 路径；打包后自动放到 exe 同目录 data/ 下。"""
    return str(data_dir() / "app.db")
