"""页面 Region 划分（方案 §3-§5）：Region → Scope → Anchor → Candidate → Value。

为什么需要 Region：
  一张发票天然分区（头部 / 购买方 / 明细表 / 合计 / 销售方），如果所有字段都在
  整页搜索，金额、税额、名称、日期这类锚点会在多处重复命中，容易串区取错值。
  （方案 §3）

模板写法::

    "regions": {
      "header": {"start_y": 0, "end_anchor": ["项目名称", "货物或应税劳务"]},
      "items":  {"start_anchor": ["项目名称"], "end_anchor": ["合 计", "合计"]},
      "totals": {"start_anchor": ["合 计", "合计"]}
    }

    "fields": {
      "invoice_no": {"region": "header", "anchor": "发票号码", ...},
      "amount":     {"region": "totals", "anchor": ["金额", "金 额"], ...}
    }

边界语义：
  · start 边界 = 起始标签所在行的最上沿（含该行；与标签同行的值不会被误伤）；
  · end 边界   = 结束标签所在行的最上沿（判定用严格小于 → 结束标签行被排除）；
  · 区域标签一个都找不到时退回全页（规则不因缺标签整体失效，与 scope 一致）。

本模块只负责"候选词的区域过滤"：锚点始终从全页词表查找（表头行常同时放着
区域标签与字段锚点），只有"取值"受限。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pdf.words import Word, merged_line_words, merged_neighbor_words

# 起始边界的浮点容差（pt）：让"与起始标签同行的值"留在区域内
_BOUNDARY_TOLERANCE = 1.0


@dataclass(frozen=True)
class RegionBounds:
    """一个区域的 y 区间（None 表示该侧不限制）。"""

    name: str
    start_y: float | None
    end_y: float | None
    resolved: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "region": self.name,
            "start_y": round(self.start_y, 2) if self.start_y is not None else None,
            "end_y": round(self.end_y, 2) if self.end_y is not None else None,
            "resolved": self.resolved,
        }


def region_bounds(
    words: list[Word], regions: dict[str, Any] | None, name: str | None
) -> RegionBounds | None:
    """解析某个区域的 y 边界；区域未配置返回 None，标签全部缺失时 resolved=False。"""
    if not regions or not name:
        return None
    config = regions.get(name)
    if not isinstance(config, dict):
        return None

    start_y = _line_y(words, config.get("start_anchor"))
    end_y = _line_y(words, config.get("end_anchor"))
    if config.get("start_y") is not None:
        value = float(config["start_y"])
        start_y = value if start_y is None else min(start_y, value)
    if config.get("end_y") is not None:
        value = float(config["end_y"])
        end_y = value if end_y is None else min(end_y, value)
    if start_y is None and end_y is None:
        return RegionBounds(name=name, start_y=None, end_y=None, resolved=False)
    return RegionBounds(name=name, start_y=start_y, end_y=end_y, resolved=True)


def filter_words(
    words: list[Word], regions: dict[str, Any] | None, name: str | None
) -> list[Word]:
    """按区域过滤候选词；区域不可用（未配置/标签缺失）时原样返回全页词表。"""
    bounds = region_bounds(words, regions, name)
    if bounds is None or not bounds.resolved:
        return words
    return [
        w
        for w in words
        if (bounds.start_y is None or w.y0 >= bounds.start_y - _BOUNDARY_TOLERANCE)
        and (bounds.end_y is None or w.y0 < bounds.end_y)
    ]


def field_words(words: list[Word], template: dict[str, Any], spec: dict[str, Any]) -> list[Word]:
    """字段候选词：模板 regions + 字段 region 的合成过滤（解析器与交叉验证共用）。"""
    return filter_words(words, template.get("regions"), spec.get("region"))


def _line_y(words: list[Word], keywords: list[str] | None) -> float | None:
    """关键词命中行的最上沿（y0）；带字间距的标签通过拼接兜底匹配。

    **三级命中的结果必须合并取 min**：直匹配可能只命中更下方的标签（如
    "价税合计（大写）"含"合计"），而上方真正需要截断的"合 计"行（字间距
    切成"合"+"计"两词）只有整行拼接才能命中——逐级短路会把区域起点错到
    更下方，漏掉合计行。
    """
    if not keywords:
        return None
    hits = [w for w in words if any(kw in w.text for kw in keywords)]
    hits.extend(
        w for w in merged_neighbor_words(words) if any(kw in w.text for kw in keywords)
    )
    hits.extend(w for w in merged_line_words(words) if any(kw in w.text for kw in keywords))
    if not hits:
        return None
    return min(w.y0 for w in hits)
