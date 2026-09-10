"""向 dev 数据库注入一条模拟的「交叉验证异常」发票记录。

用法：
    .venv\\Scripts\\python.exe scripts\\seed_mock_invoice.py

- 目标库：开发模式数据根（data/app.db）
- 幂等：按 file_hash 去重，重复运行自动跳过
- 状态 manual_review + 一条 verification_results 不匹配记录，
  用于测试「仅准确数据可导出」等按状态过滤的功能
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.db import get_engine, init_db, make_session_factory
from models.document import Document, ExtractedField, VerificationResult
from paths import default_db_path

FILE_HASH = hashlib.sha256("mock_invoice_3_cross_verify_mismatch".encode()).hexdigest()

# (field_name, raw_value, normalized_value) —— 与模板 invoice_v1 字段一致
FIELDS: list[tuple[str, str, str]] = [
    ("invoice_no", "26942000000870344356", "26942000000870344356"),
    ("invoice_date", "2026年07月22日", "2026-07-22"),
    ("buyer_name", "中磐建设集团有限公司", "中磐建设集团有限公司"),
    ("buyer_tax_no", "914114005698015827", "914114005698015827"),
    ("seller_name", "厦门仪翔建设工程有限公司", "厦门仪翔建设工程有限公司"),
    ("seller_tax_no", "913502000658790529", "913502000658790529"),
    ("item_name", "*建筑服务*劳务工程款", "*建筑服务*劳务工程款"),
    ("construction_site", "福建省厦门市湖里区仙岳医院院区", "福建省厦门市湖里区仙岳医院院区"),
    ("project_name", "厦门市仙岳医院改扩建项目地下室及上部主体工程", "厦门市仙岳医院改扩建项目地下室及上部主体工程"),
    ("tax_rate", "3%", "3%"),
    ("amount", "￥73933.20", "73933.20"),
    ("tax_amount", "￥2218.00", "2218.00"),
    ("total_amount", "￥76151.20", "76151.20"),
]


def main() -> None:
    engine = get_engine(default_db_path())
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        exists = session.query(Document).filter_by(file_hash=FILE_HASH).one_or_none()
        if exists:
            print(f"[seed] 已存在（document_id={exists.id}），跳过")
            return
        doc = Document(
            file_name="模拟_交叉验证异常_发票3.pdf",
            file_path="samples/模拟_交叉验证异常_发票3.pdf",
            file_hash=FILE_HASH,
            template_id="invoice_v1",
            template_version="v1",
            status="manual_review",
            error_reason="交叉验证不一致：tax_amount（pymupdf=2218.00 / pdfplumber=2217.99）",
        )
        doc.fields = [
            ExtractedField(field_name=k, raw_value=r, normalized_value=n)
            for k, r, n in FIELDS
        ]
        doc.verifications = [
            VerificationResult(
                field_name="tax_amount",
                primary_value="2218.00",
                secondary_value="2217.99",
                matched=False,
                review_status="pending",
            )
        ]
        session.add(doc)
        session.commit()
        print(f"[seed] OK document_id={doc.id} status={doc.status} fields={len(doc.fields)}")


if __name__ == "__main__":
    main()
