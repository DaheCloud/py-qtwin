"""pdf-project 入口。

默认启动 PySide6 GUI（MVP 第一阶段）；
--cli 保留命令行解析管线（解析 → 校验 → 交叉验证 → 落库）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Windows 控制台默认 GBK，强制 UTF-8 输出避免中文乱码
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from database.db import get_engine, init_db, make_session_factory
from paths import default_db_path, templates_dir
from pdf.template_engine import TemplateEngine
from services.pdf_service import PdfService


def run_cli(args: argparse.Namespace) -> int:
    engine = get_engine(args.db)
    init_db(engine)
    factory = make_session_factory(engine)

    engine_templates = TemplateEngine(templates_dir())
    service = PdfService(engine_templates)

    if not args.pdf:
        print("可用模板：", ", ".join(engine_templates.all) or "（templates/ 目录为空）")
        print("用法：python main.py --cli <pdf路径> [--template invoice_v1]")
        return 0

    if args.template:
        template = engine_templates.get(args.template)
        if template is None:
            print(f"模板不存在：{args.template}", file=sys.stderr)
            return 2
    else:
        template = None  # 自动识别

    with factory() as session:
        doc = service.process_document(session, args.pdf, template)
        print(f"document_id={doc.id} status={doc.status} template={doc.template_id}")
        if doc.error_reason:
            print(f"  原因：{doc.error_reason}")
        for field in doc.fields:
            print(f"  {field.field_name}: {field.normalized_value!r} (raw={field.raw_value!r})")
        for v in doc.verifications:
            print(
                f"  [verify] {v.field_name}: primary={v.primary_value!r} "
                f"secondary={v.secondary_value!r} matched={v.matched}"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="固定结构 PDF 识别工具")
    parser.add_argument("pdf", nargs="?", help="要处理的 PDF 文件路径")
    parser.add_argument("--cli", action="store_true", help="使用命令行模式而不启动 GUI")
    parser.add_argument("--template", help="手动指定模板 ID（默认自动识别）")
    parser.add_argument("--db", default=default_db_path(), help="SQLite 路径")
    args = parser.parse_args(argv)

    if args.cli:
        return run_cli(args)

    from ui.app import create_app
    from ui.main_window import MainWindow

    # GUI 模式（管理后台外壳）；如带 PDF 参数，直接走一轮上传入队
    app = create_app()
    window = MainWindow(db_path=args.db)
    window.show()
    if args.pdf:
        window._upload_page.handle_paths([args.pdf])  # noqa: SLF001 — 入口装配
    code = app.exec()
    # 后台解析线程必须在窗口销毁前停止，否则解释器退出时硬崩溃（exit 127）
    window.shutdown()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
