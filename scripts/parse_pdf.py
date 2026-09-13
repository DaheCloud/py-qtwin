"""PDF 解析诊断脚本：双引擎文本比对 + 词元坐标 + 字段级解析对照。

一次运行可以看到：
  ① 文档信息（页数/大小/元数据）
  ② PyMuPDF 整页文本（get_text("text")，含它自动插入的字间距空格）
  ③ pdfplumber 整页文本 + 表格结构（extract_tables）
  ④ 词元坐标（get_text("words")：页/行/x0/y0/x1/y1/文本，支持按 y 区间过滤）
  ⑤ 字段级解析对照（直接跑项目同一套模板/解析器/交叉验证，口径与应用完全一致）
  ⑥ 双引擎整页文本相似度 + 关键字段正则一致性

用法:
    python scripts/parse_pdf.py <pdf路径>
    python scripts/parse_pdf.py <pdf路径> --pages 1            # 只看第 1 页
    python scripts/parse_pdf.py <pdf路径> --area 200-600       # 只看表格区的词元坐标
    python scripts/parse_pdf.py <pdf路径> --template invoice_v2 # 手动指定模板
    python scripts/parse_pdf.py <pdf路径> --no-words           # 跳过词元坐标，输出更短

依赖: pymupdf / pdfplumber（项目已依赖）；rich 可选——装了输出彩色表格，
      没装自动退化为 TSV（便于直接复制粘贴）。
"""

from __future__ import annotations

import argparse
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:  # rich 可选：没有就用纯文本表格
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    console = Console()
    _RICH = True
except ImportError:  # pragma: no cover - 环境差异
    _RICH = False


# ---------------------------------------------------------------- 输出层

def rule(title: str = "") -> None:
    if _RICH:
        console.rule(f"[bold]{title}[/bold]" if title else "")
    else:
        print(f"\n{'=' * 10} {title} {'=' * 10}" if title else "=" * 40)


def panel(text: str, title: str = "") -> None:
    if _RICH:
        console.print(Panel(text, title=title, border_style="green"))
    else:
        print(f"[{title}]\n{text}" if title else text)


def table(title: str, headers: list[str], rows: list[list], style: str = "") -> None:
    """统一表格输出：rich 下彩色表格，否则 TSV（首行表头）。"""
    if _RICH:
        t = Table(title=title, title_style=f"bold {style}" if style else "bold", show_lines=True, expand=True)
        for h in headers:
            t.add_column(h, overflow="fold")
        for r in rows:
            t.add_row(*[str(c) for c in r])
        console.print(t)
    else:
        print(f"\n--- {title} ---")
        print("\t".join(headers))
        for r in rows:
            print("\t".join(str(c) for c in r))


# ---------------------------------------------------------------- 基础解析

def parse_pages(spec: str | None, total: int) -> list[int]:
    """把 '1-3,5' 这样的页码描述解析成页索引列表（内部从 0 起）。"""
    if not spec:
        return list(range(total))
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            pages.update(range(int(start) - 1, int(end)))
        else:
            pages.add(int(part) - 1)
    return sorted(p for p in pages if 0 <= p < total)


def parse_area(spec: str | None) -> tuple[float, float] | None:
    """把 '200-600' 解析成 y 区间（用于过滤词元坐标）。"""
    if not spec:
        return None
    start, _, end = spec.partition("-")
    return float(start), float(end or start)


def show_overview(pdf_path: Path, pages: list[int], total: int) -> None:
    size_kb = pdf_path.stat().st_size / 1024
    size_str = f"{size_kb / 1024:.2f} MB" if size_kb >= 1024 else f"{size_kb:.1f} KB"
    panel(
        f"文件  {pdf_path.name}\n"
        f"路径  {pdf_path.parent}\n"
        f"页数  {total}    本次解析 {len(pages)} 页    大小 {size_str}",
        title="PDF 解析报告",
    )


def show_pymupdf(path: str, pages: list[int]) -> str:
    """PyMuPDF：元数据 + 按页纯文本（get_text("text")）。"""
    import pymupdf

    rule("① PyMuPDF 识别内容")
    with pymupdf.open(path) as doc:
        meta_rows = [[k, v] for k, v in (doc.metadata or {}).items() if v]
        if meta_rows:
            table("文档元数据", ["键", "值"], meta_rows, style="cyan")
        rows = [[i + 1, doc[i].get_text("text").strip() or "(无文本)"] for i in pages]
        table("提取文本", ["页", "文本内容"], rows, style="cyan")
    return "\n".join(str(r[1]) for r in rows)


def show_pdfplumber(path: str, pages: list[int]) -> str:
    """pdfplumber：按页文本 + 表格结构。"""
    import pdfplumber

    rule("② pdfplumber 识别内容")
    text_rows: list[list] = []
    with pdfplumber.open(path) as pdf:
        table_count = 0
        for i in pages:
            if i >= len(pdf.pages):
                continue
            page = pdf.pages[i]
            text_rows.append([i + 1, (page.extract_text() or "").strip() or "(无文本)"])
            for cells in page.extract_tables():
                table_count += 1
                width = max(len(r) for r in cells)
                rows = [
                    ["" if c is None else str(c) for c in r] + [""] * (width - len(r))
                    for r in cells
                ]
                table(f"第 {i + 1} 页 · 表格 {table_count}", [f"列 {j + 1}" for j in range(width)], rows, style="magenta")
        table("提取文本", ["页", "文本内容"], text_rows, style="magenta")
    return "\n".join(str(r[1]) for r in text_rows)


def show_words(path: str, pages: list[int], area: tuple[float, float] | None) -> None:
    """词元坐标：get_text("words") 的 x0/y0/x1/y1 与文本（按 y 分组出行号）。

    版式类问题（取不到值、取到邻列）都靠这张表判断：先看目标字段的表头在哪个
    x 区间，再看数值的 x0 是否落在同一区间内。
    """
    import pymupdf

    title = "④ 词元坐标（get_text(\"words\")）"
    if area:
        title += f"  ·  y ∈ [{area[0]:g}, {area[1]:g}]"
    rule(title)

    rows: list[list] = []
    with pymupdf.open(path) as doc:
        for i in pages:
            words = sorted(doc[i].get_text("words"), key=lambda w: (round(w[1], 1), w[0]))
            line_no = 0
            last_y: float | None = None
            for x0, y0, x1, y1, text, *_ in words:
                if area and not (area[0] <= y0 <= area[1]):
                    continue
                if last_y is None or abs(y0 - last_y) > 3:
                    line_no += 1
                    last_y = y0
                rows.append([i + 1, line_no, round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1), text])
    if not rows:
        print("(没有词元，可能是扫描件或过滤区间内无内容)")
        return
    table("词元（页 / 行 / x0 / y0 / x1 / y1 / 文本）", ["页", "行", "x0", "y0", "x1", "y1", "文本"], rows)


def show_fields(path: str, template_id: str | None):
    """字段级解析对照：与应用的解析管线同源（模板 → 解析器 → pdfplumber 交叉验证）。

    返回 (template, report) 供后续文本体检复用，避免重复解析。
    """
    from pdf.dynamic_parser import DynamicRegionParser
    from pdf.pdfplumber_validator import PdfplumberValidator
    from pdf.pymupdf_parser import FixedRegionParser
    from pdf.template_engine import TemplateEngine
    from paths import templates_dir
    from ui.field_labels import field_label

    rule("⑤ 字段级解析对照（与应用的解析管线同源）")

    engine = TemplateEngine(templates_dir())
    identify_result = None
    if template_id:
        template, kind = engine.get(template_id), "manual"
    else:
        identify_result = engine.identify(path)
        template, kind = identify_result.template, identify_result.mode
    if template is None:
        print(f"未识别到模板（可用：{', '.join(engine.all) or '无'}），可用 --template 指定")
        return None, None

    if identify_result is not None and identify_result.fingerprint:
        parts = "，".join(
            f"{part['part']}={part['ratio']:.0%}" for part in identify_result.fingerprint["parts"]
        )
        variant = f" variant={identify_result.variant}" if identify_result.variant else ""
        print(f"指纹评分 {identify_result.score:.1f}（{parts}）{variant}")

    mode = template.get("mode", "fixed")
    parser = DynamicRegionParser() if mode == "dynamic" else FixedRegionParser()
    report = parser.parse(path, template)
    outcomes = {o.field_name: o for o in PdfplumberValidator().verify(path, template, report).outcomes}

    rows, failed = [], []
    for name, spec in template.get("fields", {}).items():
        result = report.fields.get(name)
        if result is None:
            continue
        outcome = outcomes.get(name)
        if outcome is None:
            second, flag = "（未参与交叉验证）", "—"
        else:
            second, flag = outcome.secondary_value or "—", ("✓" if outcome.matched else "✗")
        rule_desc = f"anchor={spec.get('anchor')!r} {spec.get('direction', 'right')}"
        if spec.get("region"):
            rule_desc = f"[{spec['region']}] " + rule_desc
        if spec.get("optional"):
            rule_desc += " optional"
        rows.append([
            field_label(name),
            result.normalized_value or "—",
            second,
            flag,
            rule_desc,
            "；".join(result.errors) or f"raw={result.raw_value!r}",
        ])
        if not result.valid:
            failed.append(field_label(name))

    table(
        f"template={template.get('template')} 识别方式={kind} mode={mode}",
        ["字段", "PyMuPDF 取值", "pdfplumber 取值", "一致", "规则", "说明/错误"],
        rows,
    )
    for err in report.business_errors:
        print(f"业务规则：{err}")
    print(
        f"\n字段 {len(rows)} 个，失败 {len(failed)} 个"
        + (f"：{'、'.join(failed)}" if failed else "（全部通过）")
    )
    return template, report


def show_table_rebuild(report) -> None:
    """表格重建结果（Table Engine）：列边界 + 逐行 items + 结构问题。"""
    result = getattr(report, "table", None)
    if result is None:
        print("\n（模板未配置 table：明细走锚点解析，无表格重建结果）")
        return

    rule("表格重建（Table Engine，方案 §6-§15）")
    col_rows = [
        [
            key,
            col.title or key,
            "✓" if col.present else "✗ 缺列",
            round(col.left, 1) if col.present else "—",
            round(col.right, 1) if col.present else "—",
        ]
        for key, col in result.columns.items()
    ]
    table("列边界（相邻表头中心点取中）", ["列", "名称", "命中", "左边界", "右边界"], col_rows)

    if result.items:
        keys = [key for key in result.columns]
        headers = ["行", *[result.columns[key].title or key for key in keys]]
        item_rows = [
            [item.get("row_index"), *[item.get(key) if item.get(key) else "—" for key in keys]]
            for item in result.items
        ]
        table(f"明细 items（{len(result.items)} 行，行关联已建立）", headers, item_rows)
    else:
        print("未重建出明细行")
    if result.issues:
        print("结构问题：" + "；".join(result.issues))


def show_audit(path: str, template, report) -> None:
    """整页文本辅助校验：无文本层检测 + 取值存在性（与应用同一套逻辑）。"""
    from pdf import text_audit

    rule("⑥ 整页文本辅助校验（无文本层 / 取值存在性）")
    if template is None or report is None:
        print("未识别到模板，跳过")
        return

    audit = text_audit.audit_document(path, template, report)
    table(
        "文本层体检",
        ["指标", "结果"],
        [
            ["文本层", "有" if audit.has_text else "无（疑似扫描件，需 OCR）"],
            ["字符数（去空白）", audit.char_count],
        ],
    )
    if not audit.has_text:
        print("文本层为空（疑似扫描件，需 OCR）→ 跳过取值存在性检查")
        return
    if audit.missing_values:
        table(
            "取值存在性 · 原始值未在原文中找到（提示，不改变文档状态）",
            ["字段", "原始值"],
            [[name, value] for name, value in audit.missing_values],
        )
    else:
        print("取值存在性：全部字段的原始值都能在原文中找到 ✓")


# ---------------------------------------------------------------- 交叉校验

# 关键字段正则（在去空白后的全文上匹配）
FIELD_PATTERNS: dict[str, str] = {
    "发票号码": r"(?<!\d)\d{20}(?!\d)",
    "开票日期": r"\d{4}年\d{1,2}月\d{1,2}日",
    "统一社会信用代码": r"(?<![0-9A-Z])[0-9A-Z]{18}(?![0-9A-Z])",
    "大写金额": r"[壹贰叁肆伍陆柒捌玖拾佰仟万亿]+[圆元]整?",
    "金额(¥)": r"¥[\d,]+(?:\.\d+)?",
}


def normalize(text: str, keep_lines: bool = False) -> str:
    """去空白处理：默认去除所有空白；keep_lines=True 时保留换行、仅去行内空白，
    避免相邻的号码与日期在拼接后粘连成一个长数字串。"""
    if keep_lines:
        return "\n".join("".join(line.split()) for line in text.splitlines())
    return "".join(text.split())


def show_verification(text_fitz: str, text_plumber: str) -> None:
    """对比两库提取结果：整体相似度 + 关键字段逐项比对。"""
    rule("⑦ 双引擎整页文本比对")
    t1 = normalize(text_fitz, keep_lines=True)
    t2 = normalize(text_plumber, keep_lines=True)
    t1_flat, t2_flat = "".join(t1.split()), "".join(t2.split())

    if not t1_flat and not t2_flat:
        ratio_str = "(两库均未提取到文本，可能是扫描件，需 OCR)"
    else:
        ratio_str = f"{SequenceMatcher(None, t1_flat, t2_flat).ratio() * 100:.1f}%"
    table(
        "整体比对",
        ["指标", "结果"],
        [["字符数（去空白后）", f"PyMuPDF {len(t1_flat)}  |  pdfplumber {len(t2_flat)}"], ["文本相似度", ratio_str]],
        style="yellow",
    )

    rows, ok = [], 0
    for name, pattern in FIELD_PATTERNS.items():
        s1 = sorted(set(re.findall(pattern, t1)))
        s2 = sorted(set(re.findall(pattern, t2)))
        if not s1 and not s2:
            result = "⚠ 均未提取"
        elif s1 == s2:
            result, ok = "✓ 一致", ok + 1
        else:
            result = "✗ 不一致"
        rows.append([name, "\n".join(s1) or "(未提取到)", "\n".join(s2) or "(未提取到)", result])
    table("关键字段比对", ["校验项", "PyMuPDF", "pdfplumber", "结果"], rows, style="yellow")
    print(f"字段一致率: {ok}/{len(FIELD_PATTERNS)}")


# ---------------------------------------------------------------- 入口

def main() -> int:
    parser = argparse.ArgumentParser(description="PDF 解析诊断：双引擎文本 + 词元坐标 + 字段解析对照")
    parser.add_argument("pdf", help="PDF 文件路径")
    parser.add_argument("--pages", help="页码范围，如 1-3 或 1,2,5（默认全部页）")
    parser.add_argument("--area", help="词元坐标只看该 y 区间，如 200-600")
    parser.add_argument("--template", help="手动指定模板 ID（默认自动识别）")
    parser.add_argument("--no-words", action="store_true", help="跳过词元坐标输出")
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.is_file():
        sys.exit(f"文件不存在: {pdf_path}")

    import pymupdf

    with pymupdf.open(str(pdf_path)) as doc:
        total = doc.page_count
    pages = parse_pages(args.pages, total)
    if not pages:
        sys.exit("没有可解析的页面，请检查 --pages 参数")

    show_overview(pdf_path, pages, total)
    text_fitz = show_pymupdf(str(pdf_path), pages)
    text_plumber = show_pdfplumber(str(pdf_path), pages)
    if not args.no_words:
        show_words(str(pdf_path), pages, parse_area(args.area))
    template, report = show_fields(str(pdf_path), args.template)
    show_table_rebuild(report)
    show_audit(str(pdf_path), template, report)
    show_verification(text_fitz, text_plumber)
    if not _RICH:
        print("\n提示：pip install rich 可获得彩色表格（当前为 TSV 输出）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
