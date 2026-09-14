"""pytest 共享 fixtures。

contract_v1 仅作为测试专用演示模板（内联定义，不依赖生产 templates/ 目录，
也不依赖磁盘上的模板 JSON 文件）。生产模板请放 templates/。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Qt 用例（筛选页交互等）统一走离屏渲染：必须在任何 Qt 导入/建 QApplication 之前设置
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pdf.template_engine import TemplateEngine  # noqa: E402

PAGE_W, PAGE_H = 595, 842

# 演示模板：与 _make_contract_pdf 生成的 PDF 坐标对齐
CONTRACT_TEMPLATE: dict = {
    "template": "contract_v1",
    "mode": "fixed",
    "dynamic_fallback": True,
    "detect": ["销售合同"],
    "page_size": [PAGE_W, PAGE_H],
    "fields": {
        "contract_no": {
            "page": 0,
            "rect": [165, 100, 320, 130],
            "anchor": "合同编号",
            "direction": "right",
            "same_line": True,
            "type": "string",
            "pattern": r"^HT\d{8}$",
            "verify": True,
        },
        "customer_name": {
            "page": 0,
            "rect": [165, 150, 350, 180],
            "anchor": "客户名称",
            "direction": "right",
            "same_line": True,
            "type": "string",
        },
        "amount": {
            "page": 0,
            "rect": [400, 300, 550, 330],
            "type": "decimal",
            "verify": True,
        },
        "sign_date": {
            "page": 0,
            "rect": [400, 350, 550, 380],
            "type": "date",
        },
    },
    "business_rules": [
        {"field": "amount", "op": ">=", "value": "0"},
        {"field": "customer_name", "op": "not_empty"},
    ],
}


@pytest.fixture
def contract_template() -> dict:
    """演示模板副本（测试可自由改写而不影响其他用例）。"""
    return {**CONTRACT_TEMPLATE}


@pytest.fixture
def contract_template_dir(tmp_path: Path) -> Path:
    """把演示模板写成 JSON 的临时目录，用于测试 TemplateEngine 的加载/识别。"""
    (tmp_path / "contract_v1.json").write_text(
        json.dumps(CONTRACT_TEMPLATE, ensure_ascii=False), encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def contract_engine(tmp_path: Path) -> TemplateEngine:
    """加载演示模板的 TemplateEngine（供服务层自动识别路径使用）。"""
    d = tmp_path / "tpl"
    d.mkdir()
    (d / "contract_v1.json").write_text(
        json.dumps(CONTRACT_TEMPLATE, ensure_ascii=False), encoding="utf-8"
    )
    return TemplateEngine(d)
