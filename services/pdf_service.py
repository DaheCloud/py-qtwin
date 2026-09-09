"""PDF 处理服务：解析 → 校验 → 交叉验证 → 事务落库。

对应 fixed_pdf_exe_tech_stack.md 第 2/16/18 节的状态流转。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from models.document import AuditLog, Document, ExtractedField, VerificationResult
from pdf.dynamic_parser import DynamicRegionParser
from pdf.pdfplumber_validator import PdfplumberValidator
from pdf.pymupdf_parser import FixedRegionParser

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_SUCCESS = "success"
STATUS_WARNING = "warning"
STATUS_FAILED = "failed"
STATUS_MANUAL_REVIEW = "manual_review"


def file_sha256(path: str | Path) -> str:
    """文件去重（第 14 节）：SHA-256 分块计算。"""
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            sha256.update(chunk)
    return sha256.hexdigest()


class PdfService:
    def __init__(self, template_engine) -> None:
        self.template_engine = template_engine
        self._parser = FixedRegionParser()
        self._dynamic_parser = DynamicRegionParser()
        self._validator = PdfplumberValidator()

    def _parse_with_fallback(self, pdf_path: str, template: dict[str, Any]):
        """按 §24 的解析优先级执行：

        mode=dynamic → 直接动态解析；
        mode=fixed   → 固定区域主解析，字段级失败且 dynamic_fallback 时逐字段兜底。
        """
        mode = template.get("mode", "fixed")
        if mode == "dynamic":
            report = self._dynamic_parser.parse(pdf_path, template)
            report.used_fallback = True
            return report

        report = self._parser.parse(pdf_path, template)
        if report.valid or not template.get("dynamic_fallback", True):
            return report

        fallback_report = self._dynamic_parser.parse(pdf_path, {**template, "mode": "dynamic"})
        merged = False
        for name, fr in report.fields.items():
            if fr.valid:
                continue
            alt = fallback_report.fields.get(name)
            if alt is not None and alt.valid:
                report.fields[name] = alt
                merged = True
        if merged:
            report.used_fallback = True
            # 合并后重跑业务规则（用兜底成功的新值）
            report.business_errors.clear()
            report.apply_business_rules(template.get("business_rules"))
        return report

    @staticmethod
    def _collect_reasons(report, mismatches: list) -> list[str]:
        """把解析报告 + 交叉验证不一致汇成人话错误原因（每条一个字段）。"""
        reasons: list[str] = []
        for name, fr in report.fields.items():
            if not fr.valid:
                raw = (fr.raw_value or "").strip()
                raw_part = f"，原始值={raw!r}" if raw else "（该区域没有提取到文本）"
                reasons.append(f"{name}：{'; '.join(fr.errors)}{raw_part}")
        reasons.extend(report.business_errors)
        for m in mismatches:
            reasons.append(
                f"{m.field_name}：双解析器不一致（PyMuPDF={m.primary_value!r} vs pdfplumber={m.secondary_value!r}）"
            )
        return reasons

    def process_document(
        self,
        session: Session,
        pdf_path: str,
        template: dict[str, Any] | None = None,
        *,
        force: bool = False,
    ) -> Document:
        """完整处理一份 PDF；document + fields + verifications 同一事务。

        force=True（手动覆盖查重）：同内容文件已导入时，删除旧记录（字段/验证
        结果级联删除）后重新导入，而不是抛错。
        """
        pdf_path = str(Path(pdf_path).resolve())
        file_hash = file_sha256(pdf_path)

        existing = session.query(Document).filter_by(file_hash=file_hash).one_or_none()
        if existing is not None:
            if not force:
                raise ValueError(f"该 PDF 已经导入（document_id={existing.id}）：{Path(pdf_path).name}")
            # 手动覆盖：旧数据随级联一起清除，保证 file_hash 唯一索引不冲突
            session.delete(existing)
            session.flush()

        if template is None:
            template = self.template_engine.detect(pdf_path)
        if template is None:
            raise ValueError("未识别到可用模板，请手动选择模板")

        doc = Document(
            file_name=Path(pdf_path).name,
            file_path=pdf_path,
            file_hash=file_hash,
            template_id=template.get("template"),
            status=STATUS_PROCESSING,
        )
        session.add(doc)
        session.flush()  # 取得 doc.id

        try:
            report = self._parse_with_fallback(pdf_path, template)
            for name, fr in report.fields.items():
                session.add(
                    ExtractedField(
                        document_id=doc.id,
                        field_name=name,
                        raw_value=fr.raw_value,
                        normalized_value=fr.normalized_value,
                        parser=fr.parser,
                    )
                )

            mismatches = []
            is_dynamic_template = template.get("mode") == "dynamic"
            verify_fields = {
                name: spec
                for name, spec in template.get("fields", {}).items()
                if spec.get("verify") and report.fields.get(name) is not None
                and report.fields[name].valid
                and (
                    # 动态模板（发票等）：pdfplumber 独立切词跑同一锚点规则二次提取
                    ("anchor" in spec if is_dynamic_template
                     # 固定模板：rect 校验只对固定解析成功的字段有意义；
                     # 被动态兜底救回的字段走人工确认。
                     else report.fields[name].parser == "pymupdf" and "rect" in spec)
                )
            }
            if report.valid and verify_fields:
                vreport = self._validator.verify(
                    pdf_path, {**template, "fields": verify_fields}, report
                )
                for outcome in vreport.outcomes:
                    session.add(
                        VerificationResult(
                            document_id=doc.id,
                            field_name=outcome.field_name,
                            primary_value=outcome.primary_value,
                            secondary_value=outcome.secondary_value,
                            matched=outcome.matched,
                            review_status="confirmed" if outcome.matched else "pending",
                        )
                    )
                mismatches = vreport.mismatches

            if not report.valid or mismatches:
                doc.status = STATUS_MANUAL_REVIEW if report.valid else STATUS_FAILED
                if report.valid and mismatches:
                    doc.status = STATUS_MANUAL_REVIEW
            else:
                doc.status = STATUS_SUCCESS

            # 失败/待人工确认时记录人话原因，UI 直接取用
            reasons = self._collect_reasons(report, mismatches)
            doc.error_reason = "；".join(reasons) if reasons else None

            session.add(
                AuditLog(
                    document_id=doc.id,
                    action="process",
                    detail=f"status={doc.status}" + (f" reasons={' | '.join(reasons)}" if reasons else ""),
                )
            )
            session.commit()
        except Exception:
            session.rollback()
            doc.status = STATUS_FAILED
            raise
        return doc
