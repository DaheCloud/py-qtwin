"""数据库模型：documents / extracted_fields / verification_results / templates / audit_logs。

对应 fixed_pdf_exe_tech_stack.md 第 9 节。SQLAlchemy 2.0 风格。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_name: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str] = mapped_column(String(1024))
    file_hash: Mapped[str] = mapped_column(String(64), index=True, unique=True)
    template_id: Mapped[str | None] = mapped_column(String(100))
    template_version: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    # V2 三维状态；status 暂时保留为兼容展示字段。
    processing_status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    quality_status: Mapped[str] = mapped_column(String(20), default="unknown", index=True)
    review_status: Mapped[str] = mapped_column(String(20), default="not_required", index=True)
    error_reason: Mapped[str | None] = mapped_column(Text)  # 人话错误/警告原因（失败/待确认时）
    # 规则式置信度（方案 §10/§31）：识别/解析/校验三维分开存 + 综合分，便于统计与审计
    identify_confidence: Mapped[int | None] = mapped_column(Integer)
    parse_confidence: Mapped[int | None] = mapped_column(Integer)
    overall_confidence: Mapped[int | None] = mapped_column(Integer)
    document_score: Mapped[float | None] = mapped_column(Float)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    fields: Mapped[list[ExtractedField]] = relationship(back_populates="document", cascade="all, delete-orphan")
    verifications: Mapped[list[VerificationResult]] = relationship(back_populates="document", cascade="all, delete-orphan")
    items: Mapped[list[ExtractedItem]] = relationship(back_populates="document", cascade="all, delete-orphan")


class ExtractedField(Base):
    __tablename__ = "extracted_fields"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    field_name: Mapped[str] = mapped_column(String(100))
    raw_value: Mapped[str | None] = mapped_column(Text)
    normalized_value: Mapped[str | None] = mapped_column(Text)
    parser: Mapped[str] = mapped_column(String(50), default="pymupdf")
    confidence: Mapped[float | None] = mapped_column(Float)
    evidence_json: Mapped[str | None] = mapped_column(Text)
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)
    strategy: Mapped[str] = mapped_column(String(30), default="primary")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    document: Mapped[Document] = relationship(back_populates="fields")


class ExtractedItem(Base):
    """明细行（Table Engine 输出，方案 §15）：行与列的关联在解析时已建立。

    与 ExtractedField 分开存：明细是"一对多"结构，逐行落库后业务校验
    （Σ明细金额≈合计金额）与 UI 明细展示都直接读这张表。
    """

    __tablename__ = "extracted_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    row_index: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str | None] = mapped_column(Text)
    spec: Mapped[str | None] = mapped_column(Text)
    unit: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[str | None] = mapped_column(Text)
    unit_price: Mapped[str | None] = mapped_column(Text)
    amount: Mapped[str | None] = mapped_column(Text)
    tax_rate: Mapped[str | None] = mapped_column(Text)
    tax: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    document: Mapped[Document] = relationship(back_populates="items")


class VerificationResult(Base):
    __tablename__ = "verification_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    field_name: Mapped[str] = mapped_column(String(100))
    primary_value: Mapped[str | None] = mapped_column(Text)
    secondary_value: Mapped[str | None] = mapped_column(Text)
    matched: Mapped[bool] = mapped_column(Boolean, default=False)
    review_status: Mapped[str] = mapped_column(String(20), default="pending")
    reviewed_value: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)

    document: Mapped[Document] = relationship(back_populates="verifications")


class TemplateRecord(Base):
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    template_id: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    version: Mapped[str] = mapped_column(String(50), default="v1")
    config_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), index=True)
    action: Mapped[str] = mapped_column(String(100))
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
