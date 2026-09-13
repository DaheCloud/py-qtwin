"""模板引擎：加载 templates/*.json，按**文档指纹**打分选出模板（方案 §16-§18）。

三层职责（方案 §16）：
  Base Schema（templates/base/*.json）—— 定义"有哪些字段"（类型/严重程度）与
      各版式共用的 regions/table/校验配置；
  Template —— 定义"怎么找"（anchor/region/table 定位规则）；
  Variant  —— 定义"哪些字段必须有"（按文档特征切换必填集合）。

识别（方案 §18）：文本指纹 30% + 锚点位置指纹 30% + 表头结构指纹 30% +
页面特征 10%，输出 0~100 的 template_score（而不是 match / no match）：

    "identify": {
      "text": {"must": [...], "any": [...], "exclude": [...]},
      "anchors": [{"text": "发票号码", "area": "top_right"}, ...],
      "table_headers": ["金额", "税率", "税额"],
      "page_size": [595.32, 841.92],
      "min_score": 60
    }

老写法（must/any/exclude/min_score、detect、fallback_detect）仍兼容：等价于
纯文本指纹（评分公式不变），新老模板不会互相干扰。
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf

from pdf.words import Word, merged_line_words, merged_neighbor_words

# 评分权重（方案 §3.3/§18）
MUST_WEIGHT = 10
ANY_WEIGHT = 2
PRIORITY_WEIGHT = 0.001

# 指纹权重（方案 §18）：文本 30 / 锚点位置 30 / 表头结构 30 / 页面特征 10
FINGERPRINT_WEIGHTS = {"text": 30, "anchors": 30, "table": 30, "page": 10}
PAGE_SIZE_TOLERANCE = 2.0  # 页面尺寸匹配容差（pt）

# 指纹 area 语义（相对页面尺寸的比例）
_AREA_TOP = 0.35
_AREA_BOTTOM = 0.65
_AREA_MID = (0.3, 0.7)
_AREA_CENTER_X = (0.35, 0.65)


@dataclass(frozen=True)
class DocFingerprint:
    """一份文档的版式特征（第一页）：文本 + 词元坐标 + 页面尺寸。

    文字 + 坐标 + 表头，比只看文本稳定（方案 §19）——两个模板都含
    "电子发票/发票号码/金额/税额"时，位置特征可以区分版式。
    """

    text: str
    words: tuple[Word, ...]
    page_width: float | None
    page_height: float | None


@dataclass(frozen=True)
class IdentifyResult:
    """一次模板识别结果：模板 + 识别方式 + 分数 + 命中明细 + 全部候选。

    审计/置信度评分都取这里的数据（identify_confidence 见 pdf/confidence.py）。
    """

    template: dict[str, Any] | None
    mode: str  # match / fallback / none / manual
    score: float = 0.0
    priority: int = 0
    matched: dict[str, list[Any]] = field(default_factory=dict)
    candidates: list[tuple[str, float]] = field(default_factory=list)
    fingerprint: dict[str, Any] | None = None
    variant: str | None = None

    @property
    def template_id(self) -> str | None:
        return self.template.get("template") if self.template else None

    def as_dict(self) -> dict[str, Any]:
        """审计日志用（结构化，不含模板正文）。"""
        data: dict[str, Any] = {
            "template": self.template_id,
            "mode": self.mode,
            "score": round(self.score, 3),
            "priority": self.priority,
            "matched": {key: list(value) for key, value in self.matched.items()},
            "candidates": [[tid, round(score, 3)] for tid, score in self.candidates],
        }
        if self.fingerprint is not None:
            data["fingerprint"] = self.fingerprint
        if self.variant is not None:
            data["variant"] = self.variant
        return data


def identify_block(template: dict[str, Any]) -> dict[str, Any]:
    """取模板的 identify 配置；老写法（detect / fallback_detect）就地兼容。"""
    identify = template.get("identify")
    if isinstance(identify, dict):
        return identify
    if template.get("fallback"):
        return {"fallback": True, "fallback_detect": template.get("fallback_detect", [])}
    return {"any": template.get("detect", [])}


def is_fingerprint(identify: dict[str, Any]) -> bool:
    """是否为指纹写法（任一非文本信号出现即视为指纹模板）。"""
    return any(key in identify for key in ("text", "anchors", "table_headers", "page_size"))


def _text_conditions(identify: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """文本条件：优先 identify.text，兼容顶层 must/any/exclude。"""
    text_cfg = identify.get("text")
    if isinstance(text_cfg, dict):
        return (
            list(text_cfg.get("must") or []),
            list(text_cfg.get("any") or []),
            list(text_cfg.get("exclude") or []),
        )
    return (
        list(identify.get("must") or []),
        list(identify.get("any") or []),
        list(identify.get("exclude") or []),
    )


def _area_ok(word: Word, area: str, page_w: float | None, page_h: float | None) -> bool:
    """词元是否落在指定页面区域（方案 §18：area 如 top_right / lower_left / left）。"""
    if page_w is None or page_h is None:
        return True  # 无页面信息时不做位置约束
    for token in str(area).lower().replace("-", "_").split("_"):
        if token in ("top", "upper"):
            if word.cy >= _AREA_TOP * page_h:
                return False
        elif token in ("bottom", "lower"):
            if word.cy <= _AREA_BOTTOM * page_h:
                return False
        elif token == "middle":
            if not (_AREA_MID[0] * page_h <= word.cy <= _AREA_MID[1] * page_h):
                return False
        elif token == "left":
            if word.cx >= 0.5 * page_w:
                return False
        elif token == "right":
            if word.cx <= 0.5 * page_w:
                return False
        elif token == "center":
            if not (_AREA_CENTER_X[0] * page_w <= word.cx <= _AREA_CENTER_X[1] * page_w):
                return False
    return True


def _find_text(words: tuple[Word, ...] | list[Word], needle: str) -> list[Word]:
    """词元文本包含匹配（含字间距拆词的拼接兜底）。"""
    if not needle:
        return []
    hits = [w for w in words if needle in w.text]
    if hits:
        return hits
    hits = [w for w in merged_neighbor_words(list(words)) if needle in w.text]
    if hits:
        return hits
    return [w for w in merged_line_words(list(words)) if needle in w.text]


def anchor_match(fp: DocFingerprint, spec: dict[str, Any] | str) -> bool:
    """锚点位置指纹：文本命中且（可选）位于指定 area。"""
    if isinstance(spec, str):
        spec = {"text": spec}
    text = str(spec.get("text") or "")
    hits = _find_text(fp.words, text)
    if not hits:
        return False
    area = spec.get("area")
    if not area:
        return True
    return any(_area_ok(word, str(area), fp.page_width, fp.page_height) for word in hits)


def header_match(fp: DocFingerprint, header: str) -> bool:
    """表头结构指纹：忽略空格差异后命中即算（"税 额" ≈ "税额"）。"""
    needle = str(header).replace(" ", "")
    if not needle:
        return False
    groups = (
        fp.words,
        merged_neighbor_words(list(fp.words)),
        merged_line_words(list(fp.words)),
    )
    return any(needle in word.text.replace(" ", "") for group in groups for word in group)


def page_size_match(fp: DocFingerprint, page_size: list[float]) -> bool:
    if len(page_size) != 2 or fp.page_width is None:
        return False
    return (
        abs(fp.page_width - float(page_size[0])) <= PAGE_SIZE_TOLERANCE
        and abs(fp.page_height - float(page_size[1])) <= PAGE_SIZE_TOLERANCE
    )


def evaluate_fingerprint(
    fp: DocFingerprint, identify: dict[str, Any]
) -> tuple[float, dict[str, Any]] | None:
    """指纹打分：0~100（方案 §18）；不适用返回 None。

    must 全命中才参评（文本硬条件）、exclude 命中即淘汰；每个配置了的信号按
    权重归一化累加（只配置文本的模板满分 100，不因缺其它信号被稀释）。
    """
    must, any_words, exclude = _text_conditions(identify)
    if any(word in fp.text for word in exclude):
        return None
    if any(word not in fp.text for word in must):
        return None

    parts: list[tuple[str, float, float, Any]] = []
    if must or any_words:
        total = len(must) * MUST_WEIGHT + len(any_words) * ANY_WEIGHT
        hits = (
            sum(MUST_WEIGHT for word in must if word in fp.text)
            + sum(ANY_WEIGHT for word in any_words if word in fp.text)
        )
        parts.append(
            (
                "text",
                FINGERPRINT_WEIGHTS["text"],
                hits / total,
                {
                    "must": [w for w in must if w in fp.text],
                    "any": [w for w in any_words if w in fp.text],
                },
            )
        )

    anchors = identify.get("anchors") or []
    if anchors:
        hit_anchors = [a for a in anchors if anchor_match(fp, a)]
        parts.append(
            (
                "anchors",
                FINGERPRINT_WEIGHTS["anchors"],
                len(hit_anchors) / len(anchors),
                {"hits": [a.get("text") if isinstance(a, dict) else a for a in hit_anchors]},
            )
        )

    table_headers = identify.get("table_headers") or []
    if table_headers:
        hits = [h for h in table_headers if header_match(fp, h)]
        parts.append(
            (
                "table",
                FINGERPRINT_WEIGHTS["table"],
                len(hits) / len(table_headers),
                {"hits": hits},
            )
        )

    page_size = identify.get("page_size") or []
    if page_size:
        ok = page_size_match(fp, page_size)
        parts.append(("page", FINGERPRINT_WEIGHTS["page"], 1.0 if ok else 0.0, {"match": ok}))

    if not parts:
        return None

    weight_sum = sum(weight for _, weight, _, _ in parts)
    score = sum(weight * ratio for _, weight, ratio, _ in parts) / weight_sum * 100
    score += int(identify.get("priority", 0)) * PRIORITY_WEIGHT
    report = {
        "parts": [
            {"part": name, "weight": weight, "ratio": round(ratio, 4), "hits": hits}
            for name, weight, ratio, hits in parts
        ],
        "weights": FINGERPRINT_WEIGHTS,
    }
    if score <= 0 or score < float(identify.get("min_score", 0)):
        return None
    return score, report


def evaluate_template(
    text: str, template: dict[str, Any], fingerprint: DocFingerprint | None = None
) -> float | None:
    """给单个专属模板打分，不适用返回 None。

    指纹写法走 0~100 指纹评分（需要 fingerprint；缺失时退化为纯文本评估）；
    老写法保持原评分公式（must×10 + any×2 + priority 微调）。
    """
    identify = identify_block(template)
    if identify.get("fallback"):
        return None

    if is_fingerprint(identify):
        if fingerprint is None:
            fingerprint = DocFingerprint(text=text, words=(), page_width=None, page_height=None)
        evaluated = evaluate_fingerprint(fingerprint, identify)
        return None if evaluated is None else evaluated[0]

    if not (identify.get("must") or identify.get("any")):
        return None  # 没配识别条件 → 不参与自动识别（与老行为一致）

    if any(word not in text for word in identify.get("must") or []):
        return None
    if any(word in text for word in identify.get("exclude") or []):
        return None

    hits = sum(1 for word in identify.get("must") or [] if word in text) + sum(
        1 for word in identify.get("any") or [] if word in text
    )
    if hits == 0:
        return None  # 关键词一个都没命中 → 不适用（老 detect 语义一致）

    score = MUST_WEIGHT * sum(1 for word in identify.get("must") or [] if word in text)
    score += ANY_WEIGHT * sum(1 for word in identify.get("any") or [] if word in text)
    score += int(identify.get("priority", 0)) * PRIORITY_WEIGHT
    if score < float(identify.get("min_score", 0)):
        return None
    return score


def matched_keywords(
    text: str, template: dict[str, Any], fingerprint: DocFingerprint | None = None
) -> dict[str, list[Any]]:
    """命中明细（审计用）：文本/锚点位置/表头结构/页面特征。"""
    identify = identify_block(template)
    must, any_words, exclude = _text_conditions(identify)
    matched: dict[str, list[Any]] = {
        "must": [word for word in must if word in text],
        "any": [word for word in any_words if word in text],
        "exclude": [word for word in exclude if word in text],
    }
    if fingerprint is not None:
        anchors = identify.get("anchors") or []
        matched["anchors"] = [
            a.get("text") if isinstance(a, dict) else a
            for a in anchors
            if anchor_match(fingerprint, a)
        ]
        table_headers = identify.get("table_headers") or []
        matched["table_headers"] = [h for h in table_headers if header_match(fingerprint, h)]
        page_size = identify.get("page_size") or []
        if page_size:
            matched["page_size"] = [page_size_match(fingerprint, page_size)]
    return matched


def fingerprint_report(fp: DocFingerprint, identify: dict[str, Any]) -> dict[str, Any] | None:
    """指纹评分明细（审计用）；非指纹模板返回 None。"""
    if not is_fingerprint(identify):
        return None
    evaluated = evaluate_fingerprint(fp, identify)
    return evaluated[1] if evaluated else {"parts": [], "weights": FINGERPRINT_WEIGHTS}


class TemplateEngine:
    """模板加载、指纹打分识别、Variant 判定与兜底选择。"""

    def __init__(self, templates_dir: str | Path = "templates") -> None:
        self.templates_dir = Path(templates_dir)
        self._templates: dict[str, dict[str, Any]] = {}
        self._bases: dict[str, dict[str, Any]] = {}
        self.reload()

    def reload(self) -> None:
        self._templates.clear()
        self._bases.clear()
        base_dir = self.templates_dir / "base"
        if base_dir.exists():
            for path in sorted(base_dir.glob("*.json")):
                with open(path, encoding="utf-8") as f:
                    schema = json.load(f)
                self._bases[schema.get("schema") or path.stem] = schema
        if not self.templates_dir.exists():
            return
        for path in sorted(self.templates_dir.glob("*.json")):
            with open(path, encoding="utf-8") as f:
                tpl = json.load(f)
            self._templates[tpl["template"]] = self._apply_base(tpl)

    # ------------------------------------------------------------ Base Schema（方案 §16）

    def _apply_base(self, template: dict[str, Any]) -> dict[str, Any]:
        """extends 合并：fields 深合并（模板覆盖 base），其余键模板未配置才继承。"""
        extends = template.get("extends")
        if not extends or extends not in self._bases:
            return template
        base = self._bases[extends]
        merged = dict(template)
        base_fields = copy.deepcopy(base.get("fields") or {})
        for name, spec in (template.get("fields") or {}).items():
            base_fields[name] = {**base_fields.get(name, {}), **spec}
        merged["fields"] = base_fields
        for key, value in base.items():
            if key in ("schema", "fields") or key in template:
                continue
            merged[key] = copy.deepcopy(value)
        return merged

    def prepare(
        self, template: dict[str, Any], text: str | None = None
    ) -> tuple[str | None, dict[str, Any]]:
        """显式传入的模板先经过 base 合并 + variant 应用（与自动识别同构）。

        返回 (variant_id, 处理后的模板副本)；text 缺省时跳过 variant 判定。
        """
        prepared = self._apply_base(dict(template))
        if text is None:
            return None, prepared
        return self._apply_variant(prepared, text)

    # ------------------------------------------------------------ Variant（方案 §16）

    def _apply_variant(
        self, template: dict[str, Any], text: str
    ) -> tuple[str | None, dict[str, Any]]:
        """按 detect 命中第一个 variant，应用 required_fields / optional_fields。"""
        variants = template.get("variants") or {}
        prepared = copy.deepcopy(template)
        if not variants:
            return None, prepared
        for variant_id, config in variants.items():
            detect = (config or {}).get("detect") or {}
            must = list(detect.get("must") or [])
            if must and not all(keyword in text for keyword in must):
                continue
            required = set((config or {}).get("required_fields") or [])
            optional = set((config or {}).get("optional_fields") or [])
            for name, spec in (prepared.get("fields") or {}).items():
                if name in required:
                    spec.pop("optional", None)
                elif name in optional:
                    spec["optional"] = True
            return variant_id, prepared
        return None, prepared

    # ------------------------------------------------------------ 查询与识别

    def get(self, template_id: str) -> dict[str, Any] | None:
        return self._templates.get(template_id)

    @property
    def all(self) -> dict[str, dict[str, Any]]:
        return dict(self._templates)

    def identify(self, pdf_path: str) -> IdentifyResult:
        """完整识别：指纹打分选最高分 → 通用兜底 → none。"""
        fp = self._fingerprint(pdf_path)

        scored: list[tuple[float, dict[str, Any]]] = []
        for tpl in self._templates.values():
            score = evaluate_template(fp.text, tpl, fingerprint=fp)
            if score is not None:
                scored.append((score, tpl))
        if scored:
            # 分数优先；同分再比 priority（指纹模式已含 priority 微调，这里做二次稳定），
            # 最后按 template id 排序，保证结果可复现，不依赖文件枚举顺序。
            scored.sort(
                key=lambda item: (
                    -item[0],
                    -int(identify_block(item[1]).get("priority", 0)),
                    item[1].get("template", ""),
                )
            )
            score, tpl = scored[0]
            variant, prepared = self._apply_variant(tpl, fp.text)
            return IdentifyResult(
                template=prepared,
                mode="match",
                score=score,
                priority=int(identify_block(tpl).get("priority", 0)),
                matched=matched_keywords(fp.text, tpl, fingerprint=fp),
                candidates=[(t["template"], s) for s, t in scored],
                fingerprint=fingerprint_report(fp, identify_block(tpl)),
                variant=variant,
            )

        fallback = self._match_fallback(fp.text)
        if fallback is not None:
            variant, prepared = self._apply_variant(fallback, fp.text)
            return IdentifyResult(
                template=prepared,
                mode="fallback",
                matched=matched_keywords(fp.text, fallback),
                variant=variant,
            )
        return IdentifyResult(template=None, mode="none")

    def resolve(self, pdf_path: str) -> tuple[dict[str, Any] | None, str]:
        """兼容旧调用：返回 (模板, 识别方式) 二元组。"""
        result = self.identify(pdf_path)
        return result.template, result.mode

    def detect(self, pdf_path: str) -> dict[str, Any] | None:
        """兼容旧调用：只做专属模板识别（不含兜底模板）。"""
        result = self.identify(pdf_path)
        return result.template if result.mode == "match" else None

    @staticmethod
    def _fingerprint(pdf_path: str) -> DocFingerprint:
        """第一页指纹：文本 + 词元坐标 + 页面尺寸（只取一次，识别全程复用）。"""
        with pymupdf.open(pdf_path) as doc:
            page = doc[0]
            words = tuple(Word(w[0], w[1], w[2], w[3], w[4]) for w in page.get_text("words"))
            return DocFingerprint(
                text=page.get_text("text"),
                words=words,
                page_width=float(page.rect.width),
                page_height=float(page.rect.height),
            )

    def _match_fallback(self, text: str) -> dict[str, Any] | None:
        """通用兜底模板：fallback_detect 未配置或任一弱关键词命中时适用。"""
        for tpl in self._templates.values():
            identify = identify_block(tpl)
            if not identify.get("fallback"):
                continue
            keywords = identify.get("fallback_detect", [])
            if keywords and not any(word in text for word in keywords):
                continue
            return tpl
        return None
