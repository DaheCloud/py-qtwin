"""解析阶段共享的 V2 上下文。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ParseContext:
    pdf_path: Path
    template: dict[str, Any] | None = None
    words: dict[int, list[Any]] = field(default_factory=dict)
    fields: dict[str, Any] = field(default_factory=dict)
    candidates: dict[str, list[Any]] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    audit: dict[str, Any] = field(default_factory=dict)
