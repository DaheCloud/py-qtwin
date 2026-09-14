"""几何位置证据（方案 §5.3-B）：取值出现的位置是否符合版式预期。

"发票号码 → 右上角"这类版式知识是最便宜的独立证据：正则正确但落在页面
底部的发票号码，几乎一定是取错了地方（例如取到备注区的另一串数字）。

预期位置来源（按优先级）：
  1. 字段 ``area``：语义位置（top_right / left / lower_left …），相对页面比例判定；
  2. 字段 ``x_min`` / ``x_max``：候选起始 x 的硬约束（购方/销方分栏）；
  3. 字段 ``page``：配置页与实际取值页不一致 → 扣分（跨页兜底取的值）。
以上都没配置时该维度**不适用**（跳过，不按 0 分算）。
"""

from __future__ import annotations

from typing import Any

from pdf.evidence.base import EVIDENCE_GEOMETRY, EvidenceItem, evidence, skipped

# area 语义 → 允许的页面比例区间 (x_lo, x_hi, y_lo, y_hi)
_AREA_BOXES: dict[str, tuple[float, float, float, float]] = {
    "top": (0.0, 1.0, 0.0, 0.35),
    "top_left": (0.0, 0.5, 0.0, 0.35),
    "top_right": (0.5, 1.0, 0.0, 0.35),
    "upper_left": (0.0, 0.5, 0.0, 0.35),
    "upper_right": (0.5, 1.0, 0.0, 0.35),
    "left": (0.0, 0.5, 0.0, 1.0),
    "right": (0.5, 1.0, 0.0, 1.0),
    "center": (0.35, 0.65, 0.3, 0.7),
    "middle": (0.35, 0.65, 0.3, 0.7),
    "bottom": (0.0, 1.0, 0.65, 1.0),
    "bottom_left": (0.0, 0.5, 0.65, 1.0),
    "lower_left": (0.0, 0.5, 0.65, 1.0),
    "bottom_right": (0.5, 1.0, 0.65, 1.0),
    "lower_right": (0.5, 1.0, 0.65, 1.0),
}

# 位置落在允许区间外时的得分（不是 0：版式可能被裁切/缩放）
_OUTSIDE_SCORE = 0.35
# 取值页与配置页不一致时的得分
_PAGE_MISMATCH_SCORE = 0.6


def geometry_evidence(
    result: Any,
    spec: dict[str, Any],
    *,
    page_width: float | None = None,
    page_height: float | None = None,
) -> EvidenceItem:
    rect = getattr(result, "rect", None)
    area = str(spec.get("area") or "").lower()
    x_min, x_max = spec.get("x_min"), spec.get("x_max")
    configured_page = spec.get("page")

    has_expectation = (
        rect is not None
        or bool(area)
        or x_min is not None
        or x_max is not None
        or configured_page is not None
    )
    if not has_expectation:
        return skipped(EVIDENCE_GEOMETRY, "未配置位置预期")

    if rect is not None:
        x0, _y0, x1, _y1 = (float(v) for v in rect)
        cx = (x0 + x1) / 2
        cy = (_y0 + _y1) / 2

        if x_min is not None and x0 < float(x_min):
            return evidence(EVIDENCE_GEOMETRY, _OUTSIDE_SCORE, f"取值 x0={x0:.1f} 小于 x_min={x_min}")
        if x_max is not None and x0 > float(x_max):
            return evidence(EVIDENCE_GEOMETRY, _OUTSIDE_SCORE, f"取值 x0={x0:.1f} 大于 x_max={x_max}")

        if area and area in _AREA_BOXES and page_width and page_height:
            x_lo, x_hi, y_lo, y_hi = _AREA_BOXES[area]
            rx, ry = cx / page_width, cy / page_height
            if x_lo <= rx <= x_hi and y_lo <= ry <= y_hi:
                return evidence(EVIDENCE_GEOMETRY, 1.0, f"位置符合预期区域 {area}")
            return evidence(EVIDENCE_GEOMETRY, _OUTSIDE_SCORE, f"位置不在预期区域 {area}")

    # 页序：取值不在配置页（跨页兜底取的值）时扣分——即使没有坐标可比对
    actual_page = getattr(result, "page", None)
    if (
        configured_page is not None
        and actual_page is not None
        and int(actual_page) != int(configured_page)
    ):
        return evidence(
            EVIDENCE_GEOMETRY,
            _PAGE_MISMATCH_SCORE,
            f"取值来自第 {int(actual_page) + 1} 页，配置页为第 {int(configured_page) + 1} 页",
        )

    if rect is not None:
        # x 范围是本字段唯一的位置约束且已通过：位置证据成立（满分）
        if x_min is not None or x_max is not None:
            return evidence(EVIDENCE_GEOMETRY, 1.0, "x 范围约束通过")
        if area:
            # 配了语义位置但没有页面尺寸可比对：保守给分，不算失败
            return evidence(EVIDENCE_GEOMETRY, 0.8, f"配置了位置区域 {area}，缺少页面尺寸无法比对")

    # 只配了 page 且页序正确：没有位置信息可加分，按"不适用"处理
    return skipped(EVIDENCE_GEOMETRY, "仅校验页序且页序正确")
