"""文档预检查（方案 §2/§30）：解析前的快速体检，决定走什么链路。

职责（方案 §35 precheck）：
  · PDF 是否损坏（能否打开）
  · 页数 / 页面尺寸
  · 是否有文本层（无文本层 → 疑似扫描件 → OCR 链路的触发条件）
  · 文本量是否异常低（关键区域完全没有文字）

OCR 触发条件（方案 §30）：第一页几乎没有文本 / 明显是扫描图片。
当前系统未接入 OCR 引擎，无文本层时直接给出"需 OCR"结论（由服务层落库）；
本模块把判定条件抽出来，后续接 OCR 时只需替换"结论"为"渲染 + 识别 →
统一 Words → 重新进入 Region / Table / Anchor 链路"。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pymupdf

# 文本层判定的最小字符数（去空白后）：完全无文本 → 疑似扫描件（方案 §30）。
# 文本层"异常低"（有字但极少）只在审计中提示，不直接触发 OCR 分流。
MIN_TEXT_CHARS = 1


@dataclass(frozen=True)
class PrecheckResult:
    """一份 PDF 的预检查结论。"""

    ok: bool  # PDF 可打开（未损坏）
    page_count: int
    page_width: float | None
    page_height: float | None
    has_text: bool  # 第一页有有效文本层
    char_count: int  # 第一页文本字符数（去空白）
    error: str | None = None

    @property
    def needs_ocr(self) -> bool:
        """无文本层 → 疑似扫描件，需要 OCR（方案 §30 触发条件）。"""
        return self.ok and not self.has_text

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "page_count": self.page_count,
            "page_size": (
                [round(self.page_width, 2), round(self.page_height, 2)]
                if self.page_width is not None
                else None
            ),
            "has_text": self.has_text,
            "char_count": self.char_count,
            "needs_ocr": self.needs_ocr,
            "error": self.error,
        }


def _strip_ws(text: str | None) -> str:
    return "".join(text.split()) if text else ""


def precheck_document(pdf_path: str | Path) -> PrecheckResult:
    """打开 PDF 做快速体检（不抛异常：损坏文件返回 ok=False + 错误信息）。"""
    try:
        with pymupdf.open(str(pdf_path)) as doc:
            page_count = doc.page_count
            first = doc[0] if page_count else None
            width = height = None
            text = ""
            if first is not None:
                rect = first.rect
                width, height = float(rect.width), float(rect.height)
                text = first.get_text("text")
            chars = len(_strip_ws(text))
            return PrecheckResult(
                ok=True,
                page_count=page_count,
                page_width=width,
                page_height=height,
                has_text=chars >= MIN_TEXT_CHARS,
                char_count=chars,
            )
    except Exception as exc:  # noqa: BLE001 — 预检查要兜住一切打开错误
        return PrecheckResult(
            ok=False,
            page_count=0,
            page_width=None,
            page_height=None,
            has_text=False,
            char_count=0,
            error=str(exc),
        )
