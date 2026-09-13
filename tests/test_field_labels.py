"""字段中文名映射回归：模板里出现过的字段必须在共享映射表里有中文名。

背景：新增字段（spec_model/unit/quantity/unit_price/item_rows）时漏补映射，
详情弹窗就回落成英文 key（用户可见的回归）。现在映射表收敛到 ui.field_labels，
本用例把"模板字段集合 ⊆ 映射表"固定下来，并校验筛选页表头也取自同一映射。

运行：.venv/Scripts/python.exe -m pytest tests/test_field_labels.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui.field_labels import FIELD_LABELS, field_label
from ui.pages.filter_page import DATA_COLUMNS

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"


def _template_field_keys() -> set[str]:
    keys: set[str] = set()
    for path in TEMPLATES_DIR.glob("*.json"):
        template = json.loads(path.read_text(encoding="utf-8"))
        keys.update(template.get("fields", {}))
    return keys


def test_every_template_field_has_chinese_label():
    missing = sorted(key for key in _template_field_keys() if key not in FIELD_LABELS)
    assert not missing, f"以下字段缺少中文显示名（界面会显示英文 key）：{missing}"


@pytest.mark.parametrize(
    "key", ["spec_model", "unit", "quantity", "unit_price", "item_rows"]
)
def test_detail_row_labels(key):
    assert key in FIELD_LABELS


def test_data_columns_share_the_same_labels():
    """筛选页列头必须与共享映射一致（列头取自 field_label，不再手写中文）。"""
    for title, key, _fallback, _width, _money in DATA_COLUMNS:
        assert title == field_label(key), f"{key} 列头与映射表不一致：{title!r}"

