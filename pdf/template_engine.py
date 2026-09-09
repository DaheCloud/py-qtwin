"""模板引擎：加载 templates/*.json，按第一页文本做模板识别。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pymupdf


class TemplateEngine:
    """模板加载与识别。

    识别规则（见 fixed_pdf_exe_tech_stack.md 第 13 节）：
      模板 JSON 的 "detect" 数组包含任意关键词即命中。
    """

    def __init__(self, templates_dir: str | Path = "templates") -> None:
        self.templates_dir = Path(templates_dir)
        self._templates: dict[str, dict[str, Any]] = {}
        self.reload()

    def reload(self) -> None:
        self._templates.clear()
        if not self.templates_dir.exists():
            return
        for path in sorted(self.templates_dir.glob("*.json")):
            with open(path, encoding="utf-8") as f:
                tpl = json.load(f)
            self._templates[tpl["template"]] = tpl

    def get(self, template_id: str) -> dict[str, Any] | None:
        return self._templates.get(template_id)

    @property
    def all(self) -> dict[str, dict[str, Any]]:
        return dict(self._templates)

    def detect(self, pdf_path: str) -> dict[str, Any] | None:
        """按第一页文本关键词识别模板；无 detect 配置的模板不参与自动识别。"""
        with pymupdf.open(pdf_path) as doc:
            text = doc[0].get_text("text")

        for tpl in self._templates.values():
            keywords = tpl.get("detect", [])
            if keywords and any(kw in text for kw in keywords):
                return tpl
        return None
