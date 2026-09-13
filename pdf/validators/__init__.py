"""校验器包：字段格式 / 结构 / 业务数学 / 双引擎交叉（方案 §14）。

  field_validator     字段格式（type / regex）+ required / critical 严重程度
  structure_validator 表格与明细结构（行数、列数、首行字段）
  business_validator  金额、税额等业务数学关系（Decimal + 容差）
  base                公共类型 CheckResult 与金额比较工具

引擎交叉验证（PyMuPDF vs pdfplumber）在 pdf/pdfplumber_validator.py，
它复用动态解析器同一套锚点规则做独立切词二次提取。
"""

from __future__ import annotations

from pdf.validators.base import (
    MONEY_TOLERANCE,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    CheckResult,
    approx_equal,
    money,
    severity_for,
    to_decimal,
    to_rate,
)
from pdf.validators.business_validator import validate_business
from pdf.validators.field_validator import (
    SEVERITY_CRITICAL,
    SEVERITY_OPTIONAL,
    SEVERITY_REQUIRED,
    FieldResult,
    apply_optional,
    field_severity,
    validate_business_rules,
    validate_field,
)
from pdf.validators.structure_validator import validate_structure

__all__ = [
    "MONEY_TOLERANCE",
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "CheckResult",
    "approx_equal",
    "money",
    "severity_for",
    "to_decimal",
    "to_rate",
    "validate_business",
    "validate_structure",
    "SEVERITY_CRITICAL",
    "SEVERITY_OPTIONAL",
    "SEVERITY_REQUIRED",
    "FieldResult",
    "apply_optional",
    "field_severity",
    "validate_business_rules",
    "validate_field",
]
