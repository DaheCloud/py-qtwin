"""PDF 处理服务：预检查 → 模板识别 → 解析 → 字段/结构/业务校验 → 双引擎交叉验证
→ 置信度评分 → 状态机 → 事务落库（方案 §11/§12/§13/§15）。

职责边界（方案 §17 原则 4）：
  Parser  —— 只负责找值（pdf/dynamic_parser.py、pdf/pymupdf_parser.py）
  Validator—— 判断值是否合理（pdf/validators/ 包：字段 / 结构 / 业务数学）
  Service  —— 编排与状态判定（本文件）

状态判定只看"问题有多严重"（方案 §11）：
  failed         critical 字段失败 / 关键业务关系不成立 / OCR 失败（接入后）
  needs_ocr      路由状态（非业务失败）：无文本层，等待 OCR 后重新解析
  manual_review  fallback 模板 / 必填字段失败 / 结构异常 / 双引擎不一致 / 置信度过低
  success        以上都没有
多行明细**不再**直接转人工复核——多行是正常的发票结构，结构异常才复核（方案 §7）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from models.document import (
    AuditLog,
    Document,
    ExtractedField,
    ExtractedItem,
    VerificationResult,
)
from pdf import text_audit
from pdf.candidates import candidate_from_result, select_best
from pdf.confidence import (
    PARSE_REVIEW_THRESHOLD,
    identify_confidence,
    need_secondary_engine,
    overall_confidence,
    parse_confidence,
    validation_confidence,
)
from pdf.dynamic_parser import DynamicRegionParser
from pdf.evidence import (
    anchor_evidence,
    business_evidence,
    cross_engine_evidence,
    document_score,
    geometry_evidence,
    pattern_evidence,
    region_evidence,
    score_field,
)
from pdf.evidence.scorer import FIELD_MEDIUM, FieldScore
from pdf.policies import FALLBACK_ANCHOR, field_fallback_policy
from pdf.pdfplumber_validator import PdfplumberValidator
from pdf.precheck import precheck_document
from pdf.parse_profiles import PARSE_DETAILED, PARSE_PROFILE_LABELS, apply_parse_profile
from pdf.pymupdf_parser import FixedRegionParser
from pdf.template_engine import IdentifyResult, TemplateEngine
from pdf.table_engine import table_score as calculate_table_score
from pdf.states import (
    PROCESSING_COMPLETED,
    PROCESSING_ERROR,
    PROCESSING_NEEDS_OCR,
    QUALITY_INVALID,
    determine_quality_status,
    determine_review_status,
    legacy_status,
)
from pdf.validators import (
    SEVERITY_CRITICAL,
    SEVERITY_ERROR,
    SEVERITY_OPTIONAL,
    CheckResult,
    field_severity,
    validate_business,
    validate_structure,
)

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_SUCCESS = "success"
STATUS_WARNING = "warning"
STATUS_FAILED = "failed"
STATUS_MANUAL_REVIEW = "manual_review"
# 路由状态（不是业务失败）：无文本层 → 等 OCR 接入后重新进入解析链路。
# OCR 成功 → 继续解析；OCR 失败才 failed（原因记 OCR 失败，而非"无文本层"）。
STATUS_NEEDS_OCR = "needs_ocr"

# 明细行数字段（模板 below_mode=count）
MULTI_ROW_FIELD = "item_rows"

# 识别方式：match = 专属模板打分命中；manual = 调用方显式指定；fallback = 通用兜底
IDENTIFY_MATCH = "match"
IDENTIFY_MANUAL = "manual"
IDENTIFY_FALLBACK = "fallback"
IDENTIFY_NONE = "none"


def field_label(name: str) -> str:
    """字段中文名（懒加载，避免 services 在导入期就依赖 ui 包）。"""
    try:
        from ui.field_labels import field_label as label

        return label(name)
    except Exception:  # noqa: BLE001 — 标签缺失不应影响解析
        return name


def file_sha256(path: str | Path) -> str:
    """文件去重（第 14 节）：SHA-256 分块计算。"""
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            sha256.update(chunk)
    return sha256.hexdigest()


def determine_status(
    *,
    critical_failed: int = 0,
    business_errors: int = 0,
    identify_mode: str = IDENTIFY_MATCH,
    cross_mismatch: int = 0,
    structure_issues: int = 0,
    business_warnings: int = 0,
    required_failed: int = 0,
    parse_confidence: int = 100,
) -> str:
    """状态机（方案 §11）：先判 failed，再判 manual_review，最后才是 success。"""
    if critical_failed or business_errors:
        return STATUS_FAILED
    if identify_mode not in (IDENTIFY_MATCH, IDENTIFY_MANUAL):
        return STATUS_MANUAL_REVIEW  # 通用兜底模板：识别不确定，需人工确认版式
    if cross_mismatch or structure_issues or required_failed or business_warnings:
        return STATUS_MANUAL_REVIEW
    if parse_confidence < PARSE_REVIEW_THRESHOLD:
        return STATUS_MANUAL_REVIEW
    return STATUS_SUCCESS


def _finding(code: str, message: str, field_name: str | None = None, **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"code": code, "message": message}
    if field_name:
        data["field"] = field_name
    data.update(extra)
    return data


@dataclass
class ProcessOutcome:
    """一次处理的完整结论（方案 §12）：errors / warnings / audit 分离。"""

    status: str
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    audit: dict[str, Any] = field(default_factory=dict)
    identify_confidence: int = 0
    parse_confidence: int = 0

    @property
    def reasons(self) -> list[str]:
        """人类可读原因（UI 的 error_reason）：错误在前、警告在后。

        advisory 项（例如可选字段没取到）只进审计与置信度，不写进用户可见原因
        ——否则一张完全正常的发票也会挂一串"未提取到值（可选字段）"。
        """
        return [
            item["message"]
            for item in (*self.errors, *self.warnings)
            if not item.get("advisory")
        ]


class PdfService:
    def __init__(self, template_engine: TemplateEngine) -> None:
        self.template_engine = template_engine
        self._parser = FixedRegionParser()
        self._dynamic_parser = DynamicRegionParser()
        self._validator = PdfplumberValidator()

    # ------------------------------------------------------------ 解析

    def _parse_with_fallback(self, pdf_path: str, template: dict[str, Any]):
        """按 §24 的解析优先级执行：

        mode=dynamic → 直接动态解析；
        mode=fixed   → 固定区域主解析，字段级失败且 dynamic_fallback 时逐字段兜底。
        """
        mode = template.get("mode", "fixed")
        if mode == "dynamic":
            return self._dynamic_parser.parse(pdf_path, template)

        report = self._parser.parse(pdf_path, template)
        fallback_names = [
            name
            for name, result in report.fields.items()
            if not result.valid
            and field_fallback_policy(template, template.get("fields", {}).get(name, {}))
            == FALLBACK_ANCHOR
        ]
        if report.valid or not fallback_names:
            return report

        fallback_report = self._dynamic_parser.parse(pdf_path, {**template, "mode": "dynamic"})
        rebuilt: list[str] = []
        for name, fr in report.fields.items():
            if name not in fallback_names:
                continue
            alt = fallback_report.fields.get(name)
            if alt is not None and alt.valid and alt.normalized_value not in (None, ""):
                report.fallback_replaced[name] = fr
                alt.fallback_used = True
                alt.strategy = "fallback"
                report.fields[name] = alt
                rebuilt.append(name)
        if rebuilt:
            report.used_fallback = True
            # 合并后重跑业务规则（用兜底成功的新值）
            report.business_errors.clear()
            report.apply_business_rules(template.get("business_rules"))
            report.rescued_fields = rebuilt
        return report

    @staticmethod
    def _item_row_count(report) -> int:
        """明细表行数；模板未配置 item_rows 字段或该字段失败时返回 0。"""
        result = report.fields.get(MULTI_ROW_FIELD)
        if result is None or not result.valid or not result.normalized_value:
            return 0
        try:
            return int(float(result.normalized_value))
        except ValueError:
            return 0

    # ------------------------------------------------------------ 校验

    @staticmethod
    def _field_findings(template: dict[str, Any], report) -> tuple[list[dict], list[dict], int]:
        """字段级结论 → (errors, warnings, optional 缺失数)（方案 §6）。

        critical 字段失败 → errors（整单 failed）
        required 字段失败 → warnings（manual_review）
        optional 字段缺失 → warnings（只提示 + 扣置信度）
        """
        errors: list[dict] = []
        warnings: list[dict] = []
        optional_missing = 0

        for name, spec in template.get("fields", {}).items():
            result = report.fields.get(name)
            if result is None:
                continue
            severity = field_severity(spec)
            if severity == SEVERITY_OPTIONAL:
                if not result.normalized_value:
                    optional_missing += 1
                    warnings.append(
                        _finding(
                            "OPTIONAL_MISSING",
                            f"{name}：未提取到值（可选字段）",
                            name,
                            advisory=True,
                        )
                    )
                continue
            if result.valid and result.normalized_value:
                continue

            raw = (result.raw_value or "").strip()
            raw_part = f"，原始值={raw!r}" if raw else "（该区域没有提取到文本）"
            detail = "；".join(result.errors) or "提取结果为空"
            message = f"{name}：{detail}{raw_part}"
            if severity == SEVERITY_CRITICAL:
                errors.append(_finding("CRITICAL_FIELD_FAILED", message, name))
            else:
                warnings.append(_finding("REQUIRED_FIELD_FAILED", message, name))

        return errors, warnings, optional_missing

    @staticmethod
    def _check_findings(checks: list[CheckResult]) -> tuple[list[dict], list[dict]]:
        """结构/业务校验结论 → (errors, warnings)；skipped 的只进审计。"""
        errors: list[dict] = []
        warnings: list[dict] = []
        for check in checks:
            if not check.failed:
                continue
            item = _finding(
                check.rule,
                check.detail or check.rule,
                severity=check.severity,
                fields=list(check.fields),
            )
            if check.severity == SEVERITY_ERROR:
                errors.append(item)
            else:
                warnings.append(item)
        return errors, warnings

    def _cross_verify(
        self, pdf_path: str, template: dict[str, Any], report, session: Session, doc: Document
    ) -> list:
        """双引擎交叉验证（PyMuPDF vs pdfplumber 独立切词跑同一套锚点规则）。"""
        is_dynamic_template = template.get("mode") == "dynamic"
        verify_fields = {
            name: spec
            for name, spec in template.get("fields", {}).items()
            if spec.get("verify")
            and report.fields.get(name) is not None
            and report.fields[name].valid
            and report.fields[name].normalized_value  # 可选字段合法为空：不参与交叉验证
            and (
                # 动态模板（发票等）：pdfplumber 独立切词跑同一锚点规则二次提取
                ("anchor" in spec if is_dynamic_template
                 # 固定模板：rect 校验只对固定解析成功的字段有意义；
                 # 被动态兜底救回的字段走人工确认。
                 else report.fields[name].parser == "pymupdf" and "rect" in spec)
            )
        }
        if not verify_fields:
            return []

        vreport = self._validator.verify(pdf_path, {**template, "fields": verify_fields}, report)
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
        return vreport.outcomes

    @staticmethod
    def _score_fields(template, report, outcomes, business_checks):
        """构建候选、融合字段证据并选出最可信结果。"""
        outcome_by_field = {outcome.field_name: outcome for outcome in outcomes}
        all_candidates: dict[str, list] = {}
        field_scores: dict[str, FieldScore] = {}

        for name, spec in template.get("fields", {}).items():
            sources: list[tuple[Any, str]] = []
            current = report.fields.get(name)
            if current is not None:
                sources.append((current, getattr(current, "strategy", "primary")))
            if name in report.table_replaced:
                sources.append((report.table_replaced[name], "primary"))
            if name in report.fallback_replaced:
                sources.append((report.fallback_replaced[name], "primary"))

            candidates = []
            seen: set[int] = set()
            for result, strategy in sources:
                if id(result) in seen:
                    continue
                seen.add(id(result))
                candidate = candidate_from_result(name, result, strategy=strategy)
                field_score = score_field(
                    name,
                    [
                        anchor_evidence(candidate, spec),
                        geometry_evidence(candidate, spec),
                        pattern_evidence(candidate, spec),
                        region_evidence(candidate, spec),
                        cross_engine_evidence(outcome_by_field.get(name)),
                        business_evidence(name, business_checks),
                    ],
                    fallback_used=candidate.fallback_used or strategy == "fallback",
                    ambiguous=candidate.candidate_count > 1,
                )
                candidate.score = field_score.score
                candidate.evidence = field_score.evidence
                candidate.reasons = field_score.reasons
                candidates.append(candidate)

            best, ordered = select_best(candidates)
            all_candidates[name] = ordered
            if best is None:
                field_scores[name] = FieldScore(name, 0.0, {}, ["没有有效候选值"])
                continue
            best.source_result.strategy = best.strategy
            best.source_result.fallback_used = best.fallback_used or best.strategy == "fallback"
            report.fields[name] = best.source_result
            field_scores[name] = FieldScore(name, best.score, best.evidence, best.reasons)
        return field_scores, all_candidates

    # ------------------------------------------------------------ 主流程

    def _fail_document(
        self,
        session: Session,
        pdf_path: str,
        file_hash: str,
        *,
        reason: str,
        code: str,
        payload: dict[str, Any] | None = None,
        status: str = STATUS_FAILED,
    ) -> Document:
        """预检查即定论的快速路径：落库 + 审计 + 返回（不进入解析链路）。

        status 缺省 failed；路由类结论（needs_ocr）传独立状态——无文本层是
        **路由条件**而非业务失败原因（OCR 接入后：needs_ocr → OCR → 成功继续
        解析 / OCR 失败才 failed）。
        """
        doc = Document(
            file_name=Path(pdf_path).name,
            file_path=pdf_path,
            file_hash=file_hash,
            status=status,
            processing_status=(
                PROCESSING_NEEDS_OCR if status == STATUS_NEEDS_OCR else PROCESSING_ERROR
            ),
            quality_status="unknown" if status == STATUS_NEEDS_OCR else QUALITY_INVALID,
            review_status="not_required" if status == STATUS_NEEDS_OCR else "pending",
            error_reason=reason,
            identify_confidence=0,
            parse_confidence=0,
            overall_confidence=0,
        )
        session.add(doc)
        session.flush()
        audit_detail = {"errors": [{"code": code, "reason": reason}], **(payload or {})}
        session.add(
            AuditLog(
                document_id=doc.id,
                action="process",
                detail=f"status={status} {json.dumps(audit_detail, ensure_ascii=False)}",
            )
        )
        session.commit()
        return doc

    def process_document(
        self,
        session: Session,
        pdf_path: str,
        template: dict[str, Any] | None = None,
        *,
        force: bool = False,
        cross_verify: bool | None = None,
        parse_profile: str = PARSE_DETAILED,
    ) -> Document:
        """完整处理一份 PDF；document + fields + items + verifications 同一事务。

        force=True（手动覆盖查重）：同内容文件已导入时，删除旧记录（字段/验证
        结果级联删除）后重新导入，而不是抛错。

        cross_verify：双引擎交叉验证的开关（None=按风险自动触发，即 Lazy Cross
        Validation；True=强制执行；False=跳过）。

        parse_profile：simple 仅基本信息与合计；detailed 保留完整明细解析。
        服务接口默认 detailed 兼容已有调用，上传页默认选择 simple。

        模板选择：显式传入 template 视为人工指定（identify.mode=manual，置信度按
        高置信处理）；否则自动识别——专属模板按指纹打分取最高分；全部淘汰落到
        通用兜底模板；连兜底都不适用才报错提示手动选择。
        """
        if parse_profile not in PARSE_PROFILE_LABELS:
            raise ValueError(f"不支持的解析模式：{parse_profile}")
        pdf_path = str(Path(pdf_path).resolve())
        file_hash = file_sha256(pdf_path)

        existing = session.query(Document).filter_by(file_hash=file_hash).one_or_none()
        if existing is not None:
            if not force:
                raise ValueError(f"该 PDF 已经导入（document_id={existing.id}）：{Path(pdf_path).name}")
            # 手动覆盖：旧数据随级联一起清除，保证 file_hash 唯一索引不冲突
            session.delete(existing)
            session.flush()

        # ① 预检查（precheck，方案 §2/§35）：损坏 / 无文本层（疑似扫描件）直接给结论，
        #    避免"未识别到模板/未找到锚点"的误导；页面尺寸等特征留给模板指纹与审计
        precheck = precheck_document(pdf_path)
        if not precheck.ok:
            return self._fail_document(
                session,
                pdf_path,
                file_hash,
                reason=f"PDF 无法打开（可能已损坏）：{precheck.error}",
                code="PDF_BROKEN",
                payload={"precheck": precheck.as_dict(), "parse_profile": parse_profile},
            )
        if precheck.needs_ocr:
            return self._fail_document(
                session,
                pdf_path,
                file_hash,
                reason="PDF 无文本层（疑似扫描件），已挂起等待 OCR 后重新解析",
                code="NEEDS_OCR",
                payload={"precheck": precheck.as_dict(), "parse_profile": parse_profile},
                status=STATUS_NEEDS_OCR,
            )

        # ② 模板识别：指纹打分选最高分，不再依赖文件名顺序
        if template is None:
            identify = self.template_engine.identify(pdf_path)
        else:
            # 显式指定：与自动识别同构——先 base 合并，再按首页文本应用 variant
            variant, prepared = self.template_engine.prepare(
                template, text_audit.page_text(pdf_path, [0])
            )
            identify = IdentifyResult(
                template=prepared, mode=IDENTIFY_MANUAL, priority=0, variant=variant
            )
        if identify.template is None:
            raise ValueError("未识别到可用模板，请手动选择模板")
        template = apply_parse_profile(identify.template, parse_profile)
        match_kind = identify.mode

        doc = Document(
            file_name=Path(pdf_path).name,
            file_path=pdf_path,
            file_hash=file_hash,
            template_id=template.get("template"),
            status=STATUS_PROCESSING,
            processing_status="processing",
            quality_status="unknown",
            review_status="not_required",
        )
        session.add(doc)
        session.flush()  # 取得 doc.id

        try:
            # ③ 字段解析（含表格重建：明细 items 行关联在解析时已建立）
            report = self._parse_with_fallback(pdf_path, template)
            items = list(getattr(report, "items", None) or [])
            for item in items:
                session.add(
                    ExtractedItem(
                        document_id=doc.id,
                        row_index=int(item.get("row_index") or 0),
                        name=item.get("name"),
                        spec=item.get("spec"),
                        unit=item.get("unit"),
                        quantity=item.get("quantity"),
                        unit_price=item.get("unit_price"),
                        amount=item.get("amount"),
                        tax_rate=item.get("tax_rate"),
                        tax=item.get("tax"),
                    )
                )

            # ④ 字段级校验（required / critical）
            errors, warnings, optional_missing = self._field_findings(template, report)
            required_failed = sum(1 for w in warnings if w["code"] == "REQUIRED_FIELD_FAILED")

            # ⑤ 业务规则（模板声明的硬约束）→ 一律判失败
            business_rule_errors = list(report.business_errors)

            # ⑥ 结构校验（明细行数 / 各列行数 / 首行字段）
            structure = validate_structure(template, report)
            structure_errors, structure_warnings = self._check_findings(structure)
            structure_issues = len(structure_errors) + len(structure_warnings)
            errors.extend(structure_errors)
            warnings.extend(structure_warnings)

            # ⑦ 业务数学校验（数量×单价、金额×税率、金额合计+税额合计≈价税合计）
            business = validate_business(template, report)
            business_errors, business_warnings = self._check_findings(business)
            errors.extend(business_errors)
            warnings.extend(business_warnings)
            for message in business_rule_errors:
                errors.append(_finding("BUSINESS_RULE_FAILED", message))

            # ⑧ 双引擎交叉验证（Lazy，方案 §26-§28）：高可信（模板可靠、字段完整、
            #    表格重建成功、校验通过）时直接成功；只有风险时才启动 pdfplumber
            #    "解决争议"。cross_verify 显式传入时以调用方为准（None=自动）。
            table = getattr(report, "table", None)
            table_failed = table is not None and not items
            ident_conf = identify_confidence(identify)
            base_parse_conf = parse_confidence(
                identify=identify,
                structure_issues=structure_issues,
                business_failures=len(business_errors),
                required_missing=required_failed,
                optional_missing=optional_missing,
            )
            critical_failed = sum(1 for e in errors if e["code"] == "CRITICAL_FIELD_FAILED")
            if cross_verify is None:
                run_secondary = need_secondary_engine(
                    identify_conf=ident_conf,
                    parse_conf=base_parse_conf,
                    structure_issues=structure_issues,
                    business_issues=len(business_errors) + len(business_warnings),
                    required_missing=required_failed,
                    critical_failed=critical_failed,
                    table_failed=table_failed,
                )
            else:
                run_secondary = cross_verify
            secondary_forced = cross_verify is True

            outcomes: list = []
            if run_secondary:
                outcomes = self._cross_verify(pdf_path, template, report, session, doc)
            mismatches = [outcome for outcome in outcomes if not outcome.matched]
            engine_matched = sum(1 for outcome in outcomes if outcome.matched)
            for mismatch in mismatches:
                warnings.append(
                    _finding(
                        "ENGINE_MISMATCH",
                        f"{mismatch.field_name}：双解析器不一致"
                        f"（PyMuPDF={mismatch.primary_value!r} vs pdfplumber={mismatch.secondary_value!r}）",
                        mismatch.field_name,
                    )
                )
            if getattr(report, "rescued_fields", None):
                # 固定模板字段失败后由锚点规则救回：取值可信度低于主路径，留痕
                warnings.append(
                    _finding(
                        "DYNAMIC_RESCUED",
                        "以下字段固定区域取值为空，已用锚点规则兜底："
                        + "、".join(report.rescued_fields),
                    )
                )

            # ⑨ 明细表：Table Engine 已逐行重建 items（多行是正常结构，不转人工）；
            #    表格重建失败时才留痕——此时明细字段回退锚点取值（能力受限）
            multi_rows = self._item_row_count(report)
            if table is not None and not items:
                warnings.append(
                    _finding(
                        "TABLE_REBUILD_FAILED",
                        "明细表未重建成功（"
                        + "、".join(table.issues[:3])
                        + "），已回退锚点取值，请人工核对明细",
                    )
                )
            elif items and multi_rows > 1 and len(items) != multi_rows:
                warnings.append(
                    _finding(
                        "ITEM_ROWS_MISMATCH",
                        f"明细行数不一致：表格重建 {len(items)} 行 vs 行数统计 {multi_rows} 行，请人工核对",
                    )
                )

            # ⑩ 整页文本体检（advisory，只提示不判状态）
            audit = text_audit.audit_document(pdf_path, template, report)
            if audit.missing_values:
                preview = "、".join(f"{name}={value!r}" for name, value in audit.missing_values)
                warnings.append(
                    _finding("VALUE_NOT_IN_TEXT", f"取值存在性提示：{preview} 未在原文文本中找到，请人工核对")
                )

            # ⑪ 识别方式留痕 + 置信度评分（识别/解析/校验三维 + 综合，方案 §31/§32）
            if match_kind == IDENTIFY_FALLBACK:
                warnings.insert(
                    0,
                    _finding(
                        "TEMPLATE_FALLBACK",
                        f"未识别到专属模板，已使用通用锚点模板（{template.get('template')}）解析，需重点复核",
                    ),
                )
            field_scores, candidates = self._score_fields(template, report, outcomes, business)
            critical_names = [
                name for name, spec in template.get("fields", {}).items()
                if field_severity(spec) == SEVERITY_CRITICAL
            ]
            required_names = [
                name for name, spec in template.get("fields", {}).items()
                if field_severity(spec) == "required"
            ]
            critical_values = [field_scores[name].score for name in critical_names if name in field_scores]
            required_values = [field_scores[name].score for name in required_names if name in field_scores]
            critical_min = min(critical_values, default=0.0 if critical_names else 1.0)
            required_avg = (
                sum(required_values) / len(required_values)
                if required_values else (0.0 if required_names else 1.0)
            )
            evaluated_business = [check for check in business if not check.skipped]
            business_score = (
                sum(1.0 if check.passed else (0.0 if check.severity == SEVERITY_ERROR else 0.4)
                    for check in evaluated_business) / len(evaluated_business)
                if evaluated_business else None
            )
            table_quality = calculate_table_score(table, template.get("table"))
            doc_score = document_score(
                critical_min=critical_min,
                required_avg=required_avg,
                table_score=table_quality,
                business_score=business_score,
                identify_score=ident_conf / 100.0,
            )
            parse_conf = round(
                100 * sum(score.score for score in field_scores.values()) / len(field_scores)
            ) if field_scores else 0
            validation_conf = validation_confidence(
                business_failures=len(business_errors),
                business_warnings=len(business_warnings),
                structure_issues=structure_issues,
                cross_mismatch=len(mismatches),
            )
            overall_conf = round(doc_score * 100)

            # ⑫ 状态机
            quality_status = determine_quality_status(
                critical_invalid=bool(critical_failed or business_errors or business_rule_errors),
                critical_min=critical_min,
                required_low=bool(required_failed) or any(
                    field_scores[name].score < FIELD_MEDIUM
                    for name in required_names if name in field_scores
                ),
                document_score=doc_score,
                identify_weak=match_kind not in (IDENTIFY_MATCH, IDENTIFY_MANUAL),
                has_flags=bool(mismatches or structure_issues or business_warnings or report.rescued_fields),
            )
            review_status = determine_review_status(quality_status)
            status = legacy_status(
                processing_status=PROCESSING_COMPLETED,
                quality_status=quality_status,
                review_status=review_status,
            )
            doc.status = status
            doc.processing_status = PROCESSING_COMPLETED
            doc.quality_status = quality_status
            doc.review_status = review_status
            doc.document_score = doc_score
            doc.identify_confidence = ident_conf
            doc.parse_confidence = parse_conf
            doc.overall_confidence = overall_conf
            reason_text = "；".join(
                ProcessOutcome(status=status, errors=errors, warnings=warnings).reasons
            )
            doc.error_reason = reason_text or None

            for name, result in report.fields.items():
                scored = field_scores.get(name, FieldScore(name, 0.0))
                session.add(
                    ExtractedField(
                        document_id=doc.id,
                        field_name=name,
                        raw_value=result.raw_value,
                        normalized_value=result.normalized_value,
                        parser=result.parser,
                        confidence=scored.score,
                        evidence_json=json.dumps(scored.as_dict(), ensure_ascii=False),
                        fallback_used=bool(getattr(result, "fallback_used", False)),
                        strategy=str(getattr(result, "strategy", "primary")),
                    )
                )

            # ⑬ 结构化审计（方案 §13）：识别 / 解析 / 校验三段，JSON 落库
            audit_payload = {
                "identify": identify.as_dict(),
                "precheck": precheck.as_dict(),
                "parse": {
                    "profile": parse_profile,
                    "engine": "pymupdf",
                    "template_mode": template.get("mode"),
                    "field_success": sum(1 for f in report.fields.values() if f.valid),
                    "field_failed": sum(1 for f in report.fields.values() if not f.valid),
                    "multi_rows": multi_rows,
                    # 表格重建（方案 §15）：列边界/结构问题/逐行 items 全量留痕
                    "items": len(items),
                    "table_applied": list(getattr(report, "table_applied", None) or []),
                    "table": (
                        {key: value for key, value in table.as_dict().items() if key != "items"}
                        if table is not None
                        else None
                    ),
                },
                "verify": {
                    # Lazy Cross Validation（方案 §26-§28）：触发与否、原因留痕
                    "secondary_engine": {
                        "triggered": run_secondary,
                        "forced": secondary_forced,
                        "base_parse_confidence": base_parse_conf,
                    },
                    "engine_match": engine_matched,
                    "engine_mismatch": len(mismatches),
                    "business_passed": sum(1 for c in business if c.passed and not c.skipped),
                    "business_failed": len(business_errors) + len(business_warnings),
                    "business_skipped": sum(1 for c in business if c.skipped),
                    "structure_failed": structure_issues,
                },
                "structure": [check.as_dict() for check in structure],
                "business": [check.as_dict() for check in business],
                "text": {
                    "has_text": audit.has_text,
                    "char_count": audit.char_count,
                    "similarity": round(audit.similarity, 4) if audit.similarity is not None else None,
                    "value_not_in_text": len(audit.missing_values),
                },
                "errors": [item["code"] for item in errors],
                "warnings": [item["code"] for item in warnings],
                "identify_confidence": ident_conf,
                "parse_confidence": parse_conf,
                "validation_confidence": validation_conf,
                "overall_confidence": overall_conf,
                "field_scores": {name: score.as_dict() for name, score in field_scores.items()},
                "candidates": {
                    name: [candidate.as_dict() for candidate in values]
                    for name, values in candidates.items()
                },
                "document_score": round(doc_score, 4),
                "states": {
                    "processing_status": doc.processing_status,
                    "quality_status": doc.quality_status,
                    "review_status": doc.review_status,
                    "legacy_status": doc.status,
                },
            }
            session.add(
                AuditLog(
                    document_id=doc.id,
                    action="process",
                    detail=f"status={status} {json.dumps(audit_payload, ensure_ascii=False)}",
                )
            )
            session.commit()
        except Exception:
            session.rollback()
            doc.status = STATUS_FAILED
            raise
        return doc
