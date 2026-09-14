"""SQLite 结构迁移：老库自动升级，打包升级不丢数据。

为什么需要这个模块
------------------
桌面端打包后的数据库是"跨版本长期存活"的：用户在 v1 建的 app.db 会在 v2/v3
的 exe 里继续被使用。而 SQLAlchemy 的 ``create_all`` **只建缺失的表，不会给
已存在的表补列**。早期用一张手写的补列清单兜底，一旦漏登记（``template_version``
与 ``error_reason`` 都漏了），升级后任何 ``select(Document)`` 都会抛：

    OperationalError: no such column: documents.template_version

表现就是用户说的"打包后数据库结构变了，打开失败"。

做法
----
1. **以 SQLAlchemy 模型为唯一事实来源逐表对齐**：缺表建表、缺列补列、
   缺索引补索引。以后再加字段不用改这里，天然覆盖；
2. ``PRAGMA user_version`` 记录结构版本：升级前自动备份库文件；
   发现库比程序更新（旧 exe 打开新库）时明确拒绝，避免写坏新结构；
3. 文件损坏（中断拷贝、被别的程序写过、根本不是 SQLite 文件）时
   把原文件隔离备份并重建空库——宁可丢历史，也要能打开程序。

数据库结构版本历史
------------------
* v1：documents / extracted_fields / verification_results / templates / audit_logs
* v2：documents 增列（template_version、error_reason、三个 confidence 列）；
       新增 extracted_items（Table Engine 明细行，方案 §15）
* v3：V2 三维状态、文档质量分，以及字段 evidence/fallback/strategy
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.exc import DatabaseError, SQLAlchemyError
from sqlalchemy.schema import CreateIndex

from models.document import Base

# 当前程序期望的结构版本（每次改动 model/表结构都要 +1）
SCHEMA_VERSION = 3

_DIALECT = sqlite_dialect.dialect()

# 已做过完整性检查的库文件（同一进程内只查一次）。
# PRAGMA quick_check 会扫全库，而 init_db 在每次列表刷新时都会被调用；
# 表/列/索引的对齐很轻（只看 PRAGMA），每次都做，保证结构永远是最新的。
_INTEGRITY_CHECKED: set[str] = set()


class SchemaError(RuntimeError):
    """结构迁移失败（无法安全打开数据库）。"""


class SchemaTooNewError(SchemaError):
    """数据库由更新版本的程序创建：本程序不认识，拒绝打开，避免写坏数据。"""


@dataclass
class MigrationReport:
    """一次结构对齐的结果（启动日志/错误提示/测试断言都用它）。"""

    db_path: str = ":memory:"
    existed: bool = False
    from_version: int = 0
    to_version: int = SCHEMA_VERSION
    created_tables: list[str] = field(default_factory=list)
    added_columns: list[str] = field(default_factory=list)
    created_indexes: list[str] = field(default_factory=list)
    backup_path: str | None = None
    corrupt_backup: str | None = None
    rebuilt: bool = False
    issues: list[str] = field(default_factory=list)

    @property
    def migrated(self) -> bool:
        """本次是否真的改动了结构（用于决定要不要打日志/提示）。"""
        return bool(
            self.created_tables
            or self.added_columns
            or self.created_indexes
            or self.rebuilt
            or self.from_version != self.to_version
        )

    def summary(self) -> str:
        parts: list[str] = []
        if self.rebuilt:
            parts.append("库文件损坏已重建")
        if self.existed and self.from_version != self.to_version:
            parts.append(f"结构版本 v{self.from_version} → v{self.to_version}")

        tables = list(self.created_tables)
        if not self.existed and not self.rebuilt:
            # 全新库：列全表比版本号更直观
            parts.append("新建数据库" + (f"（{len(tables)} 张表）" if tables else ""))
            tables = []
        if tables:
            parts.append("新增表 " + "、".join(tables))
        if self.added_columns:
            parts.append(f"补齐列 {len(self.added_columns)} 个（" + "、".join(self.added_columns) + "）")
        if self.backup_path:
            parts.append(f"原库已备份为 {Path(self.backup_path).name}")
        if self.corrupt_backup:
            parts.append(f"损坏文件保留为 {Path(self.corrupt_backup).name}")
        return "；".join(parts) if parts else "结构无变化"


# --------------------------------------------------------------------- 入口


def upgrade(engine, *, backup: bool = True, rebuild_corrupt: bool = True) -> MigrationReport:
    """把 ``engine`` 指向的库升级到当前结构版本（幂等，可重复调用）。

    步骤：版本检查 → 完整性检查 → 备份 → 逐表对齐 → 写回版本号。
    任何一步失败都抛 :class:`SchemaError`，调用方负责提示用户
    （数据库文件本身不会被破坏：对齐只做 CREATE / ADD COLUMN）。
    """
    path = _db_file(engine)
    report = MigrationReport(db_path=str(path) if path else ":memory:")
    report.existed = bool(path and path.exists() and path.stat().st_size > 0)

    if report.existed and _need_integrity_check(path):
        problem = _integrity_problem(engine)
        if problem:
            if not rebuild_corrupt:
                raise SchemaError(f"数据库文件损坏（{problem}）：{path}")
            engine.dispose()
            report.issues.append(f"数据库文件损坏（{problem}）")
            report.corrupt_backup = _str(_quarantine_corrupt(path))
            report.rebuilt = True
            report.existed = False

    version = _read_version(engine) if report.existed else 0
    report.from_version = version
    if version > SCHEMA_VERSION:
        raise SchemaTooNewError(
            f"数据库结构版本 v{version} 高于当前程序支持的 v{SCHEMA_VERSION}："
            "请使用新版本程序打开（旧程序写入可能损坏数据）"
        )

    if report.existed and backup and version < SCHEMA_VERSION:
        report.backup_path = _str(backup_database(path, tag=f"v{version}"))

    changes = _reconcile(engine)
    _backfill_v3(engine, changes.added_columns)
    report.created_tables = changes.created_tables
    report.added_columns = changes.added_columns
    report.created_indexes = changes.created_indexes
    report.issues.extend(changes.issues)

    if version != SCHEMA_VERSION:
        _write_version(engine, SCHEMA_VERSION)
    report.to_version = SCHEMA_VERSION
    return report


def _backfill_v3(engine, added_columns: list[str]) -> None:
    """根据旧 status 补齐历史行的三维状态，保留机器原始结论。"""
    state_columns = {
        "documents.processing_status",
        "documents.quality_status",
        "documents.review_status",
    }
    if not state_columns.intersection(added_columns):
        return
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE documents
            SET processing_status = CASE
                    WHEN status = 'needs_ocr' THEN 'needs_ocr'
                    WHEN status = 'failed' THEN 'completed'
                    ELSE 'completed'
                END,
                quality_status = CASE
                    WHEN status = 'success' THEN 'valid'
                    WHEN status IN ('manual_review', 'warning') THEN 'warning'
                    WHEN status = 'failed' THEN 'invalid'
                    ELSE 'unknown'
                END,
                review_status = CASE
                    WHEN status IN ('manual_review', 'warning', 'failed') THEN 'pending'
                    ELSE 'not_required'
                END
        """))


def backup_database(path: Path, *, tag: str) -> Path | None:
    """把库文件（含 -wal/-shm）复制为 ``<db>.bak-<tag>``。

    已存在同名备份时不覆盖：保留最早（最接近升级前原始状态）的那份，
    因为对齐只做 CREATE/ADD COLUMN，可叠加回放，原始备份更安全。
    """
    target = path.with_name(f"{path.name}.bak-{tag}")
    if target.exists():
        return target
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        for suffix in ("-wal", "-shm"):
            side = path.with_name(f"{path.name}{suffix}")
            if side.exists():
                shutil.copy2(side, target.with_name(f"{target.name}{suffix}"))
    except OSError:
        return None
    return target


# ------------------------------------------------------------------ 内部实现


@dataclass
class _Changes:
    created_tables: list[str] = field(default_factory=list)
    added_columns: list[str] = field(default_factory=list)
    created_indexes: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


def _db_file(engine) -> Path | None:
    """取文件型 SQLite 的路径；内存库/其它驱动返回 None（不做文件级操作）。"""
    try:
        if engine.url.get_backend_name() != "sqlite":
            return None
    except AttributeError:
        return None
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    return Path(database)


def _str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _need_integrity_check(path: Path | None) -> bool:
    """该库文件是否需要做完整性检查（同进程同路径只查一次）。"""
    if path is None:
        return True
    key = str(path.resolve())
    if key in _INTEGRITY_CHECKED:
        return False
    _INTEGRITY_CHECKED.add(key)
    return True


def _read_version(engine) -> int:
    try:
        with engine.connect() as conn:
            return int(conn.execute(text("PRAGMA user_version")).scalar() or 0)
    except DatabaseError as exc:  # 不是 SQLite 文件 / 结构损坏
        raise SchemaError(f"无法读取数据库结构版本：{str(exc).splitlines()[0]}") from exc


def _write_version(engine, version: int) -> None:
    with engine.begin() as conn:
        conn.execute(text(f"PRAGMA user_version = {int(version)}"))


def _integrity_problem(engine) -> str | None:
    """``PRAGMA quick_check``；返回问题描述，None 表示正常。"""
    try:
        with engine.connect() as conn:
            result = conn.execute(text("PRAGMA quick_check")).scalar()
    except SQLAlchemyError as exc:
        return str(exc).splitlines()[0]
    except Exception as exc:  # noqa: BLE001 - 任何读取异常都按损坏处理
        return str(exc).splitlines()[0]
    if result is None:
        return None
    text_result = str(result).strip()
    return None if text_result.lower() == "ok" else text_result


def _quarantine_corrupt(path: Path) -> Path | None:
    """把损坏的库挪到 ``<db>.corrupt-<时间戳>``（不删除，留给用户抢救数据）。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        path.replace(target)
        for suffix in ("-wal", "-shm"):
            side = path.with_name(f"{path.name}{suffix}")
            if side.exists():
                side.unlink()
    except OSError:
        return None
    return target


def _reconcile(engine) -> _Changes:
    """以模型为准逐表对齐：补表 → 补列 → 补索引。"""
    changes = _Changes()
    meta = Base.metadata

    before = set(inspect(engine).get_table_names())
    changes.created_tables = [t.name for t in meta.sorted_tables if t.name not in before]
    meta.create_all(engine)  # 缺表（含其索引/约束）一次建齐

    # create_all 会新建表，需要重新取一次快照
    tables = set(inspect(engine).get_table_names())
    inspector = inspect(engine)

    for table in meta.sorted_tables:
        if table.name not in tables:
            continue
        present = {col["name"] for col in inspector.get_columns(table.name)}
        missing = [col for col in table.columns if col.name not in present]
        if missing:
            with engine.begin() as conn:
                for column in missing:
                    conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {_column_ddl(column)}"))
                    changes.added_columns.append(f"{table.name}.{column.name}")
        changes.created_indexes.extend(_ensure_indexes(engine, table, changes))

    return changes


def _ensure_indexes(engine, table, changes: _Changes) -> list[str]:
    """给"本来就存在、因此被 create_all 跳过"的表补索引。

    已有唯一索引覆盖了相同列时不重复建（如 file_hash 的 UNIQUE 约束索引）。
    """
    created: list[str] = []
    try:
        existing = inspect(engine).get_indexes(table.name)
    except SQLAlchemyError as exc:
        changes.issues.append(f"{table.name} 索引读取失败：{str(exc).splitlines()[0]}")
        return created

    names = {idx["name"] for idx in existing}
    unique_columns = {
        tuple(idx.get("column_names") or ()) for idx in existing if idx.get("unique")
    }
    for index in table.indexes:
        if not index.name or index.name in names:
            continue
        columns = tuple(col.name for col in index.columns)
        if index.unique and columns in unique_columns:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(str(CreateIndex(index, if_not_exists=True).compile(dialect=_DIALECT))))
            created.append(index.name)
        except SQLAlchemyError as exc:
            # 索引补不上不影响数据读取，记进 issues 即可，不阻断启动
            changes.issues.append(f"{index.name} 创建失败：{str(exc).splitlines()[0]}")
    return created


def _column_ddl(column) -> str:
    """按模型列生成 SQLite 的 ``ADD COLUMN`` 片段（列名 + 类型 + 可能的默认值）。

    模型里 ``nullable=False`` 但没写标量默认值（如 ``default=utcnow``）的列，
    SQLite 不允许无默认值追加非空列，这里统一按可空补齐：历史行留 NULL，
    新行由 ORM 正常写入，不影响读取。
    """
    ddl = f"{column.name} {column.type.compile(dialect=_DIALECT)}"
    literal = _default_literal(column)
    if literal is not None and not column.nullable:
        ddl += f" NOT NULL DEFAULT {literal}"
    return ddl


def _default_literal(column) -> str | None:
    """把模型里的标量默认值渲染成 SQL 字面量；函数型默认值（utcnow）返回 None。"""
    default = column.default
    if default is None or not getattr(default, "is_scalar", False):
        return None
    value = default.arg
    if callable(value):
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"
