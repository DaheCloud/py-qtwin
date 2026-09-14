"""V2 字段兜底与 Region 失败策略。"""

from __future__ import annotations

from typing import Any

FALLBACK_NONE = "none"
FALLBACK_ANCHOR = "anchor"
FALLBACK_TABLE = "table"
FALLBACK_PAGE = "page"
FALLBACK_DOCUMENT = "document"
FALLBACK_POLICIES = {
    FALLBACK_NONE,
    FALLBACK_ANCHOR,
    FALLBACK_TABLE,
    FALLBACK_PAGE,
    FALLBACK_DOCUMENT,
}

REGION_FAIL = "fail"
REGION_PAGE = "page"
REGION_DOCUMENT = "document"
REGION_POLICIES = {REGION_FAIL, REGION_PAGE, REGION_DOCUMENT}


def _policy(value: Any, allowed: set[str], name: str) -> str:
    resolved = str(value).strip().lower()
    if resolved not in allowed:
        raise ValueError(f"不支持的 {name}={value!r}，可选值：{sorted(allowed)}")
    return resolved


def field_fallback_policy(template: dict[str, Any], spec: dict[str, Any]) -> str:
    """字段配置优先；旧 ``dynamic_fallback=true`` 仅映射为 anchor。"""
    if "fallback_policy" in spec:
        return _policy(spec["fallback_policy"], FALLBACK_POLICIES, "fallback_policy")
    if "dynamic_fallback" in spec:
        return FALLBACK_ANCHOR if spec["dynamic_fallback"] else FALLBACK_NONE
    if "fallback_policy" in template:
        return _policy(template["fallback_policy"], FALLBACK_POLICIES, "fallback_policy")
    if template.get("dynamic_fallback", False):
        return FALLBACK_ANCHOR
    return FALLBACK_NONE


def field_region_policy(template: dict[str, Any], spec: dict[str, Any]) -> str:
    """解析 Region 失败策略；默认拒绝扩大搜索范围。"""
    value = spec.get(
        "region_failure_policy",
        template.get("region_failure_policy", REGION_FAIL),
    )
    return _policy(value, REGION_POLICIES, "region_failure_policy")
