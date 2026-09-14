"""Region 证据（方案 §4.3）：取值是否真的落在预期区域内。

V1：区域标签缺失 → 静默退回全页，金额可能从明细行或备注区取到"合法但错误"的数。
V2：回退行为由 region_failure_policy 决定，并且**回退本身要被记录成证据扣分**
（方案 §4.3 的 RegionEvidence）。

得分：
  1.00  区域已解析且取值受限在该区域内
  0.55  退回当前页全页（policy=page）
  0.40  退回全文/跨页兜底（policy=document）
  0.25  配置了 region 但取值来源未知（不应出现，保守给分）
不适用（跳过）：字段未配置 region，或取值来自 Table Engine（区域由表格
自身定位，逐字段区域检查已无意义——表格的位置证据由 table evidence 承担）。
"""

from __future__ import annotations

from typing import Any

from pdf.evidence.base import EVIDENCE_REGION, EvidenceItem, evidence, skipped
from pdf.policies import REGION_DOCUMENT, REGION_PAGE


def region_evidence(result: Any, spec: dict[str, Any]) -> EvidenceItem:
    region = spec.get("region")
    if not region:
        return skipped(EVIDENCE_REGION, "未配置 region")

    strategy = str(getattr(result, "strategy", "") or "")
    parser = str(getattr(result, "parser", "") or "")
    if strategy == "table" or parser.endswith("-table"):
        return skipped(EVIDENCE_REGION, "取值来自明细表格重建，区域由表格定位")

    fallback = getattr(result, "region_fallback", None)
    if fallback == REGION_DOCUMENT:
        return evidence(EVIDENCE_REGION, 0.40, f"区域 {region} 未解析，已退回全文取值")
    if fallback == REGION_PAGE:
        return evidence(EVIDENCE_REGION, 0.55, f"区域 {region} 未解析，已退回全页取值")

    if getattr(result, "region_used", False):
        return evidence(EVIDENCE_REGION, 1.0, f"取值限定在区域 {region}")
    return evidence(EVIDENCE_REGION, 0.25, f"配置了区域 {region} 但取值来源未知")
