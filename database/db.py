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
    """开发阶段直接 create_all；接入 Alembic 后由迁移管理。

    create_all 不会给已存在的表补列，所以新增列在这里用 ALTER TABLE 补齐
    （SQLite 支持 ADD COLUMN，老库升级不需要清空数据）。
    """
    Base.metadata.create_all(engine)
    _ensure_columns(engine)


# 新增列的最小迁移：表名 → {列名: 建表用的类型}
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "documents": {
        "identify_confidence": "INTEGER",
        "parse_confidence": "INTEGER",
        "overall_confidence": "INTEGER",
    },
}


def _ensure_columns(engine) -> None:
    """给已存在的表补上后加的列（缺什么补什么，已存在则跳过）。"""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue
            present = {col["name"] for col in inspector.get_columns(table)}
            for column, coltype in columns.items():
                if column not in present:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"))


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
