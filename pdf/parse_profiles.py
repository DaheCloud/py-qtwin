"""上传解析范围：在模板识别、继承与变体应用后裁剪明细配置。"""

from copy import deepcopy
from typing import Any

PARSE_SIMPLE = "simple"
PARSE_DETAILED = "detailed"
PARSE_PROFILE_LABELS = {PARSE_SIMPLE: "简化解析", PARSE_DETAILED: "详细解析"}

SIMPLE_HIDDEN_FIELDS = {
    "item_name", "spec_model", "unit", "quantity", "unit_price", "tax_rate",
    "item_rows", "item_amount_rows", "item_tax_rows",
    "construction_site", "project_name",
}


def apply_parse_profile(template: dict[str, Any], profile: str) -> dict[str, Any]:
    """简化模式不提取明细、不重建表格；保留基本信息及合计校验。"""
    if profile not in PARSE_PROFILE_LABELS:
        raise ValueError(f"不支持的解析模式：{profile}")
    if profile == PARSE_DETAILED:
        return template

    prepared = deepcopy(template)
    prepared["fields"] = {
        name: spec for name, spec in prepared.get("fields", {}).items()
        if name not in SIMPLE_HIDDEN_FIELDS and spec.get("region") != "items"
    }
    prepared.pop("table", None)
    prepared.get("structure", {}).pop("item_table", None)
    business = prepared.get("business_check", {})
    business.pop("item", None)
    business.pop("rows_field", None)
    prepared["business_rules"] = [
        rule for rule in prepared.get("business_rules", [])
        if rule.get("field") in prepared["fields"]
    ]
    return prepared
