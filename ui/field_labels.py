"""字段元信息：字段 key → 中文显示名（界面唯一来源）。

数据筛选页的列定义（filter_page.DATA_COLUMNS）、导出表头、详情弹窗的字段名
都取自这里，避免多处各维护一份映射造成漂移（曾出现"表格显示中文、弹窗显示
英文 key"的问题）。未登记的字段回落为原始 key。
"""

from __future__ import annotations

FIELD_LABELS: dict[str, str] = {
    # 发票字段（与 templates/*.json 的字段集合保持一致）
    "invoice_no": "发票号码",
    "invoice_date": "开票日期",
    "buyer_name": "购买方名称",
    "buyer_tax_no": "购买方税号",
    "seller_name": "销售方名称",
    "seller_tax_no": "销售方税号",
    "item_name": "项目名称",
    "spec_model": "规格型号",
    "unit": "单位",
    "quantity": "数量",
    "unit_price": "单价",
    "item_rows": "明细行数",
    "item_amount_rows": "金额列行数",
    "item_tax_rows": "税额列行数",
    "construction_site": "建筑服务发生地",
    "project_name": "建筑项目名称",
    "tax_rate": "税率",
    "amount": "金额",
    "tax_amount": "税额",
    "total_amount": "价税合计",
    # 旧合同数据（历史记录兼容显示）
    "contract_no": "合同编号",
    "customer_name": "客户名称",
    "sign_date": "签订日期",
}


def field_label(name: str) -> str:
    """取字段中文名；未登记时原样返回 key（便于发现漏登记）。"""
    return FIELD_LABELS.get(name, name)
