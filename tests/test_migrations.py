"""数据库结构迁移测试：老库（尤其是打包升级遗留的库）必须能自动打开。

回归的线上问题（用户反馈"打包后数据库结构变了，打开失败"）：
    老版本打包出的 app.db 里 documents 表没有 template_version / error_reason
    等后加的列，而 create_all 只管建表不补列 → 升级后列表页任何
    select(Document) 都抛 "no such column: documents.template_version"。
    修复：以模型为唯一事实来源逐表对齐（补表/补列/补索引）+ 版本号 + 备份。

运行：.venv/Scripts/python.exe -m pytest tests/test_migrations.py -v
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from sqlalchemy import inspect, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.db import ensure_schema, get_engine, init_db, make_session_factory
from database.migrations import (
    SCHEMA_VERSION,
    SchemaTooNewError,
    upgrade,
)
from models.document import Base, Document

# 老版本（v1）的建表语句：documents 缺 template_version / error_reason / 置信度列，
# 也没有 extracted_items 表；索引也没建
LEGACY_V1 = """
CREATE TABLE documents (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    file_name VARCHAR(255) NOT NULL,
    file_path VARCHAR(1024) NOT NULL,
    file_hash VARCHAR(64) NOT NULL UNIQUE,
    template_id VARCHAR(100),
    status VARCHAR(20),
    imported_at DATETIME,
    created_at DATETIME,
    updated_at DATETIME
);
CREATE TABLE extracted_fields (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER,
    field_name VARCHAR(100) NOT NULL,
    raw_value TEXT,
    normalized_value TEXT,
    parser VARCHAR(50),
    confidence FLOAT,
    created_at DATETIME
);
CREATE TABLE verification_results (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER,
    field_name VARCHAR(100) NOT NULL,
    primary_value TEXT,
    secondary_value TEXT,
    matched BOOLEAN,
    review_status VARCHAR(20),
    reviewed_value TEXT,
    reviewed_at DATETIME
);
CREATE TABLE templates (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    template_id VARCHAR(100) NOT NULL UNIQUE,
    name VARCHAR(255) NOT NULL,
    version VARCHAR(50),
    config_json TEXT NOT NULL,
    created_at DATETIME
);
CREATE TABLE audit_logs (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER,
    action VARCHAR(100) NOT NULL,
    detail TEXT,
    created_at DATETIME
);
INSERT INTO documents (file_name, file_path, file_hash, template_id, status)
VALUES ('old.pdf', 'C:/old.pdf', 'hash-legacy', 'invoice_v1', 'success');
INSERT INTO extracted_fields (document_id, field_name, raw_value, normalized_value, parser)
VALUES (1, 'grand_total', '¥1,356.00', '1356.00', 'pymupdf');
"""


def _make_legacy_db(path: Path) -> Path:
    """造一个"老版本 exe 留下的 app.db"。"""
    con = sqlite3.connect(path)
    con.executescript(LEGACY_V1)
    con.commit()
    con.close()
    return path


def _columns(engine, table: str) -> set[str]:
    return {col["name"] for col in inspect(engine).get_columns(table)}


def _tables(engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _user_version(path: Path) -> int:
    con = sqlite3.connect(path)
    try:
        return int(con.execute("PRAGMA user_version").fetchone()[0])
    finally:
        con.close()


class TestLegacyUpgrade:
    def test_missing_columns_added_and_old_data_kept(self, tmp_path):
        """核心回归：老库缺列时自动补齐，且列表页查询不再抛 no such column。"""
        db = _make_legacy_db(tmp_path / "app.db")
        engine = get_engine(db)

        report = init_db(engine)

        # 模型里有的列，库里有有——与 metadata 完全对齐
        expected = {col.name for col in Document.__table__.columns}
        assert expected <= _columns(engine, "documents")
        assert {"documents.template_version", "documents.error_reason"} <= set(report.added_columns)
        # 新表（Table Engine 明细）被建出来
        assert "extracted_items" in _tables(engine)
        assert report.rebuilt is False
        # 结构版本写回
        assert report.from_version == 0 and report.to_version == SCHEMA_VERSION
        assert _user_version(db) == SCHEMA_VERSION
        # 迁移前自动备份，且备份就是老库（不含新列）
        assert report.backup_path is not None
        backup = Path(report.backup_path)
        assert backup.exists() and backup.name == "app.db.bak-v0"
        legacy_columns = {row[1] for row in sqlite3.connect(backup).execute("PRAGMA table_info(documents)")}
        assert "template_version" not in legacy_columns

        # 老数据完好，ORM 能正常读（含 relationship）
        with make_session_factory(engine)() as session:
            doc = session.scalars(select(Document)).one()
            assert doc.file_name == "old.pdf"
            assert doc.status == "success"
            assert doc.template_version is None  # 历史行留空，不是报错
            assert doc.error_reason is None
            assert doc.items == []
            assert [f.normalized_value for f in doc.fields] == ["1356.00"]

    def test_legacy_indexes_created(self, tmp_path):
        """老表被 create_all 跳过 → 其索引也要单独补齐。"""
        db = _make_legacy_db(tmp_path / "app.db")
        engine = get_engine(db)

        init_db(engine)

        names = {idx["name"] for idx in inspect(engine).get_indexes("documents")}
        assert "ix_documents_status" in names
        assert "ix_documents_file_hash" in names

    def test_idempotent_second_run_changes_nothing(self, tmp_path):
        db = _make_legacy_db(tmp_path / "app.db")
        engine = get_engine(db)

        first = init_db(engine)
        second = init_db(engine)

        assert first.migrated is True
        assert second.migrated is False
        assert second.added_columns == []
        assert second.created_tables == []
        assert second.backup_path is None  # 版本已是最新，不再重复备份
        assert second.summary() == "结构无变化"

    def test_missing_table_is_recreated(self, tmp_path):
        """表被误删（或早期版本没有）时，下次启动自动补回来。"""
        db = tmp_path / "app.db"
        engine = get_engine(db)
        init_db(engine)
        with engine.begin() as conn:
            conn.execute(__import__("sqlalchemy").text("DROP TABLE extracted_items"))

        report = init_db(engine)

        assert "extracted_items" in report.created_tables
        assert "extracted_items" in _tables(engine)


class TestFreshDatabase:
    def test_new_file_creates_full_schema_without_backup(self, tmp_path):
        db = tmp_path / "app.db"
        engine = get_engine(db)

        report = init_db(engine)

        assert _tables(engine) == set(Base.metadata.tables)
        assert report.backup_path is None
        assert report.existed is False
        assert _user_version(db) == SCHEMA_VERSION
        assert not list(tmp_path.glob("*.bak-*"))
        # 全新库只报"新建数据库"，不该出现 v0 → v2 这种版本号噪声
        assert "v0" not in report.summary()

    def test_zero_byte_file_is_treated_as_new(self, tmp_path):
        """中断拷贝留下的 0 字节文件不应被判为损坏库。"""
        db = tmp_path / "app.db"
        db.write_bytes(b"")
        engine = get_engine(db)

        report = init_db(engine)

        assert report.rebuilt is False
        assert _tables(engine) == set(Base.metadata.tables)

    def test_ensure_schema_accepts_path(self, tmp_path):
        report = ensure_schema(tmp_path / "app.db")
        assert report.to_version == SCHEMA_VERSION

    def test_memory_database(self):
        engine = get_engine(":memory:")
        report = init_db(engine)
        assert report.db_path == ":memory:"
        assert report.backup_path is None
        assert "extracted_items" in _tables(engine)


class TestBrokenDatabase:
    def test_corrupt_file_is_quarantined_and_rebuilt(self, tmp_path):
        """不是 SQLite 文件（被别的程序覆盖/损坏）时：隔离原文件 + 重建空库，程序要能开。"""
        db = tmp_path / "app.db"
        db.write_bytes(b"this is not a sqlite database at all" * 10)
        engine = get_engine(db)

        report = init_db(engine)

        assert report.rebuilt is True
        assert report.corrupt_backup is not None
        quarantined = Path(report.corrupt_backup)
        assert quarantined.read_bytes().startswith(b"this is not")
        assert _tables(engine) == set(Base.metadata.tables)
        with make_session_factory(engine)() as session:
            session.add(
                Document(file_name="new.pdf", file_path="C:/new.pdf", file_hash="h-new")
            )
            session.commit()
            assert session.scalars(select(Document)).one().file_name == "new.pdf"

    def test_schema_too_new_is_rejected_without_touching_file(self, tmp_path):
        """旧程序打开新库：明确拒绝，且不改动对方的文件。"""
        db = tmp_path / "app.db"
        engine = get_engine(db)
        init_db(engine)
        con = sqlite3.connect(db)
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
        con.commit()
        con.close()
        before = db.read_bytes()

        with pytest.raises(SchemaTooNewError) as exc:
            upgrade(engine)

        assert "高于当前程序" in str(exc.value)
        assert _user_version(db) == SCHEMA_VERSION + 5
        assert db.read_bytes() == before
        assert not list(tmp_path.glob("*.bak-*"))


class TestReport:
    def test_summary_mentions_what_changed(self, tmp_path):
        db = _make_legacy_db(tmp_path / "app.db")
        report = init_db(get_engine(db))
        summary = report.summary()
        assert "结构版本 v0 → v2" in summary
        assert "extracted_items" in summary
        assert "app.db.bak-v0" in summary
        assert report.migrated is True
