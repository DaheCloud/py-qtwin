"""数据库会话与初始化：UI → Service → Repository → SQLAlchemy → SQLite。

对应 fixed_pdf_exe_tech_stack.md 第 17/18 节。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from models.document import Base

_DEFAULT_DB = "data/app.db"


def get_engine(db_path: str | Path = _DEFAULT_DB):
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path.as_posix()}", echo=False)
    return engine


def init_db(engine) -> None:
    """开发阶段直接 create_all；接入 Alembic 后由迁移管理。"""
    Base.metadata.create_all(engine)


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
