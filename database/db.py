"""数据库会话与初始化：UI → Service → Repository → SQLAlchemy → SQLite。

对应 fixed_pdf_exe_tech_stack.md 第 17/18 节。

结构迁移（老库升级）不在本模块，见 :mod:`database.migrations`：
``init_db`` 只是它的薄封装，好处是打包升级后老 app.db 能自动补齐
新增的表/列，不会出现 "no such column" 打不开列表的情况。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from database.migrations import MigrationReport, SchemaError, SchemaTooNewError, upgrade
from models.document import Base

_DEFAULT_DB = "data/app.db"

__all__ = [
    "Base",
    "MigrationReport",
    "SchemaError",
    "SchemaTooNewError",
    "ensure_schema",
    "get_engine",
    "init_db",
    "make_session_factory",
]


def get_engine(db_path: str | Path = _DEFAULT_DB):
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    # timeout：后台解析线程与 UI 线程并发写库时，等锁而不是立刻抛
    # "database is locked"（打包后单机使用，30s 足够）
    engine = create_engine(
        f"sqlite:///{path.as_posix()}",
        echo=False,
        connect_args={"timeout": 30},
    )
    return engine


def init_db(engine) -> MigrationReport:
    """确保结构是最新的（幂等）：建表 + 对齐新增列/索引 + 记录结构版本。

    老库（甚至是从未打过版本号的库）会被自动升级，升级前自动留一份
    ``app.db.bak-v<旧版本>`` 备份；库文件损坏时隔离原文件并重建。
    失败抛 :class:`SchemaError` / :class:`SchemaTooNewError`，由入口提示用户。
    """
    return upgrade(engine)


def ensure_schema(db_path: str | Path = _DEFAULT_DB) -> MigrationReport:
    """按路径建引擎并完成结构对齐（入口启动时调用一次）。"""
    return init_db(get_engine(db_path))


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
