# PdfDataTool 技术架构总览

> 本文档基于**当前代码库实际实现**整理（逐文件核对），不是规划稿。
> 演进背景与历史方案见根目录 `fixed_pdf_exe_tech_stack.md`（早期技术选型）与
> `pdf_hybrid_parsing_architecture.md`（混合解析架构基线）。
> 数据存放位置见 `docs/data-storage.md`。

---

## 1. 项目定位

**固定结构 PDF（以增值税发票为主）关键字段识别与管理系统**，单机 Windows 桌面应用：

- 输入：带文本层的 PDF（发票、合同等固定/半固定版式）
- 处理：预检查 → 模板指纹识别 → 区域/锚点/表格三引擎解析 → 标准化 → 三层校验 → 置信度评分 → 状态机 → 事务落库
- 输出：结构化字段 + 逐行明细，本地 SQLite 持久化；支持筛选、导出 Excel、人工确认复核、PDF 编辑
- 交付：PyInstaller `onedir` + `--windowed` 打包为绿色文件夹版 EXE

核心设计取向：**规则驱动、可解释、可审计**——不使用机器学习/OCR 主链路，所有结论都留下扣分项、命中项与审计 JSON。

---

## 2. 技术栈清单

### 2.1 运行/框架

| 类别 | 技术 | 版本 | 实际职责 |
|---|---|---|---|
| 语言 | Python | 3.x（venv `.venv`） | — |
| GUI 框架 | **PySide6 (Qt 6)** | 6.11.2 | 主窗口、四页面、表格、PDF 预览、PDF 编辑器 |
| PDF 主引擎 | **PyMuPDF** | 1.28.2 | 取词、坐标、固定区域提取、跨页画布、PDF 编辑 |
| PDF 交叉验证引擎 | **pdfplumber** | 0.11.10 | 独立切词二次提取（Lazy 触发）、诊断脚本整页对照 |
| ORM | **SQLAlchemy 2.0** | 2.0.52 | Declarative 模型 + Session 事务（UI→Service→SQLAlchemy→SQLite） |
| 数据库 | **SQLite** | 内置 | 单机持久化，`connect_args={"timeout": 30}` 抗锁 |
| 数据库迁移 | **自研** `database/migrations.py` | SCHEMA_VERSION = 2 | 以模型为事实来源逐表对齐 + `PRAGMA user_version` + 自动备份 |
| Excel 导出 | **openpyxl** | 3.1.5 | 筛选页批量导出 `.xlsx`（表头样式/列宽/冻结） |
| 日志 | **标准库 logging** | — | `%APPDATA%\PdfDataTool\logs\app.log` + stderr |
| 打包 | **PyInstaller** | — | `--onedir --windowed`，`templates/` 随包分发 |
| 测试 | **pytest** | 9.1.1 | 16 个测试模块；Qt 用例 `QT_QPA_PLATFORM=offscreen` |

### 2.2 依赖现状说明（与清单不一致处）

`requirements.txt` 中存在但**代码未引用**的依赖：

- `alembic` / `Mako`：迁移实际由 `database/migrations.py` 自研实现，未使用 Alembic；
- `loguru`：日志实际用标准库 `logging`（`ui/bootstrap.py`）；
- `pydantic` / `pydantic_core`：模板为普通 `dict` + JSON，未做 Pydantic 强类型校验。

上述为早期技术选型的残留依赖，不影响运行；如需精简可按上表清理。

---

## 3. 分层架构

```
┌───────────────────────────────────────────────────────────────────┐
│  表现层  ui/                                            PySide6   │
│  MainWindow(侧边栏 + QStackedWidget 四页)                          │
│   upload_page │ filter_page │ settings_page │ pdf_editor_page     │
│   detail_dialog（PDF 预览 + 字段/明细/校验结果确认）                │
│   widgets/(Toast/Badge/Delegates)  styles.py(QSS 主题 Token)      │
└──────────────────────────────┬────────────────────────────────────┘
                               │ 直接调用（无网络层；后台解析走 QThread）
┌──────────────────────────────▼────────────────────────────────────┐
│  服务层  services/                                                 │
│   pdf_service.PdfService     编排 + 状态机 + 事务落库 + Lazy 交叉  │
│   review_service             人工确认（筛选页一键确认 / 弹窗确认） │
└──────────────────────────────┬────────────────────────────────────┘
┌──────────────────────────────▼────────────────────────────────────┐
│  解析引擎层  pdf/                                                  │
│   words（统一词元） → precheck → template_engine（指纹识别）       │
│   → region_engine（分区）→ dynamic_parser（锚点）/ table_engine    │
│   → normalizers → validators/{field,structure,business}            │
│   → confidence（三维 + Lazy 触发）→ pdfplumber_validator（第二引擎）│
│   → text_audit（整页文本 advisory）                                │
└──────────────────────────────┬────────────────────────────────────┘
┌──────────────────────────────▼────────────────────────────────────┐
│  数据层  models/ + database/                                       │
│   Document / ExtractedField / ExtractedItem / VerificationResult   │
│   / TemplateRecord / AuditLog                                      │
│   db.py（engine/session）· migrations.py（结构对齐/备份/损坏重建）  │
└──────────────────────────────┬────────────────────────────────────┘
                               ▼
                     SQLite (app.db)
                               ▲
┌──────────────────────────────┴────────────────────────────────────┐
│  配置/资源  templates/{base,invoice_*.json} · paths.py · assets/   │
└───────────────────────────────────────────────────────────────────┘
```

`repositories/` 目前为空（`__init__.py` 0 字节）：当前查询直接由服务层/页面层使用 SQLAlchemy 语句完成，Repository 层为预留扩展位。

---

## 4. 目录结构（实际）

```
py-qtwin/
├─ main.py                    入口：GUI 默认 / --cli 命令行管线
├─ paths.py                   只读资源根 base_dir / 可写数据根 data_dir / 日志路径
├─ PdfDataTool.spec           PyInstaller 规格（datas=[('templates','templates')]）
├─ build_exe.bat              一键打包（备份→打包→还原 dist 内 data）
├─ run_gui.bat                双击启动 GUI（PYTHONUTF8=1）
│
├─ ui/
│  ├─ app.py                  create_app()/run_app()：QApplication + 主题 + 启动引导
│  ├─ bootstrap.py            setup_logging / install_excepthook / bootstrap_database
│  ├─ main_window.py          侧边栏 + 四页 QStackedWidget + 页面联动 + shutdown
│  ├─ field_labels.py         字段 key → 中文名（界面唯一来源，避免多处漂移）
│  ├─ styles.py               QSS 主题（light/dark/system）+ 颜色 Token + 图标生成
│  ├─ pages/
│  │  ├─ upload_page.py       拖拽/点选入队 + QThread 后台解析 + 状态徽章
│  │  ├─ filter_page.py       数据表格：筛选/月份/批量导出/删除/一键确认/详情
│  │  ├─ detail_dialog.py     QtPdf 预览 + 字段/明细/交叉验证 + 人工确认
│  │  ├─ pdf_editor_page.py   QGraphicsView 自绘 PDF 编辑器（加字/水印/涂黑/旋转/增删页）
│  │  └─ settings_page.py     主题、置信度阈值、dynamic_fallback（QSettings INI）
│  └─ widgets/{shell.py, common.py}
│
├─ services/{pdf_service.py, review_service.py}
│
├─ pdf/
│  ├─ words.py                统一词元 Word + 行聚类/拼接/切词工具
│  ├─ precheck.py             损坏/页数/尺寸/文本层 → needs_ocr 路由
│  ├─ template_engine.py      指纹打分识别 + base 深合并 + variant 切换
│  ├─ region_engine.py        Region y 区间解析与候选过滤
│  ├─ dynamic_parser.py       Anchor Engine + 跨页取值 extract_field_cross_page
│  ├─ table_engine.py         Table Engine（表头→列边界→物理行→逻辑行→items）
│  ├─ pymupdf_parser.py       固定区域解析 rect（FixedRegionParser + ParseReport）
│  ├─ pdfplumber_validator.py 第二引擎交叉验证
│  ├─ normalizers.py          金额/日期/文本标准化
│  ├─ confidence.py           三维置信度 + Lazy 触发判定
│  ├─ text_audit.py           整页文本体检（取值存在性/相似度，advisory）
│  └─ validators/{base,field_validator,structure_validator,business_validator}.py
│
├─ models/document.py         6 张表的 SQLAlchemy 2.0 模型
├─ database/{db.py, migrations.py}
├─ templates/{base/invoice.json, invoice_v1.json, invoice_v2.json, invoice_generic.json}
├─ tests/                     16 个测试模块 + 样例 PDF + ui.html 设计稿
├─ scripts/                   parse_pdf.py（诊断）· ui_smoke.py · ui_screenshot.py
│                             · gen_app_icon.py · seed_mock_invoice.py · clean_registry_settings.bat
└─ docs/                      本文档 + data-storage.md + 界面截图
```

---

## 5. 启动与装配流程

### 5.1 GUI（默认）

```
main.py
 ├─ ensure_data_dir()                       建数据目录 + 旧版 exe 同目录 data/ 一次性迁移
 ├─ setup_logging()                         app.log + stderr
 ├─ install_excepthook()                    未捕获异常写日志（windowed 无控制台）
 ├─ argparse：pdf? / --cli / --template / --db
 └─ ui.app.create_app()
      ├─ apply_theme(app, get_theme_choice(load_settings()))   light/dark/system
      ├─ app.setWindowIcon / setFont(ui_font(10))
      └─ bootstrap_database(db)              ensure_schema：老库自动补齐表/列/索引
           ├─ SchemaTooNewError → 人话提示「数据库版本过新」并退出码 3
           └─ SchemaError       → 提示「数据库无法打开」并退出码 3
      └─ MainWindow(db_path=...) → show()
           └─ app.exec()
      └─ window.shutdown()                   必须停后台 QThread，否则退出硬崩（exit 127）
```

### 5.2 CLI（`--cli`）

`main.py::run_cli`：`bootstrap_database` → `TemplateEngine(templates_dir())` → `PdfService`
→ `service.process_document(session, pdf, template)` → 打印 `document_id/status/template`、
逐字段 `normalized_value (raw=…)`、逐条 `[verify] primary/secondary/matched`。

无参数运行时打印可用模板清单与用法。

---

## 6. 核心处理链路（`PdfService.process_document`）

```
0  文件 SHA-256 计算 + 查重           同 hash 已存在 → 抛错；force=True → 级联删除旧记录重导
1  precheck_document(pdf)
     ├─ 打不开 → failed(PDF_BROKEN)，落库即返回
     └─ needs_ocr（任意页均无文本）→ 状态 needs_ocr（路由状态，非业务失败）
2  模板识别
     ├─ 未指定 → TemplateEngine.identify(pdf)：四信号指纹 0~100 取最高分
     │            全淘汰 → invoice_generic（fallback，识别分 60、进人工复核）
     └─ 显式指定 → TemplateEngine.prepare(template, 首页文本)：base 深合并 + variant
3  建 Document(status=processing) + flush 取 id
4  解析 _parse_with_fallback
     ├─ mode=dynamic → DynamicRegionParser（Anchor + Table）
     └─ mode=fixed   → FixedRegionParser(rect)；字段级失败且 dynamic_fallback
                        → 动态兜底逐字段救回（rescued_fields 留痕），重跑 business_rules
     产出 ParseReport：fields / items / table / business_errors / used_fallback
5  落库 ExtractedField（逐字段）+ ExtractedItem（逐行明细）
6  字段级结论 _field_findings：critical→errors；required→warnings；optional 缺失→advisory
7  模板 business_rules（report.business_errors）→ 一律 errors
8  结构校验 validate_structure（表头/必需列/逐行关键单元格）→ errors/warnings
9  业务数学校验 validate_business（逐行 + Σ + 价税合计，Decimal 容差）
10 Lazy 交叉验证
     need_secondary_engine(identify_conf, base_parse_conf, structure/business/required/
                           critical/table_failed) → True 才跑 pdfplumber
     cross_verify: None=按风险自动 / True=强制 / False=跳过
     不一致 → ENGINE_MISMATCH 警告 + 写 VerificationResult
11 表格重建体检：table 存在但 items 为空 → TABLE_REBUILD_FAILED（已回退锚点取值）
     items 数与 item_rows 统计不一致 → ITEM_ROWS_MISMATCH
12 text_audit.audit_document：取值存在性（advisory，只警告不改状态）
13 识别方式留痕（fallback 模板置顶警告）+ 三维置信度 + overall（30/40/30）
14 determine_status → 写 documents.status / 三维置信度 / error_reason
15 结构化审计 AuditLog(detail=JSON)：identify / precheck / parse / verify / structure /
     business / text / errors / warnings / 四个置信度分
16 session.commit()；异常 → rollback 并置 failed 后重新抛出
```

---

## 7. 解析引擎层详解

### 7.1 统一词元层 `pdf/words.py`

所有取词来源（PyMuPDF 文本层、pdfplumber、未来 OCR）统一为 `Word(x0, y0, x1, y1, text)`（frozen dataclass），
提供行聚类 `group_rows`、同行拼接 `merged_line_words`、相邻拼接 `merged_neighbor_words`、
跨页画布 `merged_page_words`、数值碎片合并 `with_number_fragments`、行数统计 `count_rows` 等。

关键常量：`ANCHOR_MERGE_GAP_RATIO = 1.6`（吸收「单 位」「税 额」字间距拆词）、
`TRAILING_PLACEHOLDERS`（清除单元格占位符「—」）、`NUMBER_FRAGMENT`（金额被切成多段数字）。

> 三类引擎共用同一份 Words 口径，因此"换取词引擎"不会导致规则分叉。

### 7.2 预检查 `pdf/precheck.py`

输出 `PrecheckResult`：`ok / page_count / page_width / page_height / has_text / char_count / error`，
派生属性 `needs_ocr = ok and not has_text`。文本层按**任意页有文本**判定（多页文档首页可能是图片封面），
达标即提前停止扫描。

### 7.3 模板指纹识别 `pdf/template_engine.py`

四类信号加权（归一化到 0~100，只配置部分信号时按已配置权重归一）：

| 信号 | 权重 | 说明 |
|---|---|---|
| `text` | 30% | `must`（全命中才参评）/ `any` / `exclude` |
| `anchors` | 30% | 文本 + `area` 位置（`top_right` / `left` / `lower_left` …） |
| `table_headers` | 30% | 表头结构（忽略字间距空格差异） |
| `page_size` | 10% | 页面尺寸匹配，容差 ±2pt |

另有 `priority` 微权重（0.001）用于同分裁决，`min_score` 为入围门槛（常用 60）。
老写法（`must/any/exclude/min_score`、`detect`、`fallback_detect`）完全兼容，等价于纯文本指纹。

`IdentifyResult` 携带 `template / mode(match|manual|fallback|none) / score / matched / candidates /
fingerprint / variant`，直接供置信度与审计使用。

### 7.4 页面分区 `pdf/region_engine.py`

模板声明 `regions: {header / items / totals}`，`field_words()` 为字段候选词做 y 区间过滤：

- start 边界 = 起始标签行最上沿（含该行，同行值不被误伤，容差 1.0pt）
- end 边界 = 结束标签行最上沿（严格小于 → 结束行被排除）
- 区域标签全部缺失 → 退回全页（规则不因缺标签整体失效）
- **锚点始终从全页词表查找**（表头行常同时承载区域标签与字段锚点），只有"取值候选"受限

### 7.5 锚点引擎 `pdf/dynamic_parser.py`

字段定位键：

| 键 | 说明 |
|---|---|
| `region` | 取值候选限定区域（header / items / totals） |
| `scope` | `{start_anchor, end_anchor}` 更细的取值分区 |
| `anchor` | 字符串或候选列表（按序尝试，首个成功返回） |
| `direction` | `right`（同行右侧）/ `below`（下方） |
| `anchor_span` | 锚点 x 窗口向右延伸到本列右边界（吸收表头拆词碎片） |
| `below_mode` | `prefix` / `merge`（跨行单元格）/ `each` / `row`（多段数值拼接）/ `count`（行数统计） |
| `stop_anchor` | 截断关键词（排除合计行/信息块） |
| `pick` | `first` / `last`（合计行在明细下方时取列最下方值） |
| `x_min` / `x_max` | 候选 x 范围（区分购方/销方分栏） |
| `max_distance` | 锚点到值的最大距离 |
| `pattern` / `type` / `verify` | 格式校验 / 标准化类型 / 是否参与交叉验证 |

候选按"与锚点的行/列重叠质量"分层：`_STRICT_OVERLAP_RATIO = 0.5` 以上为严格同行/同列优先，
擦边候选兜底；`below` 的贴合度按"列窗口覆盖度"计算，避免单字标签（「金」）误判。

跨页取值 `extract_field_cross_page`：配置页优先 → 页序兜底（带 region 的字段在某页 region 不可解析则跳过该页）→
"配置页 + 后续页"扩展画布重试；PyMuPDF 与 pdfplumber **共用同一策略**，保证双引擎口径一致。

### 7.6 表格引擎 `pdf/table_engine.py`（Table First）

```
找表头（候选表头 + 字间距拆词兜底）
→ 列边界 = 相邻表头中心点取中
→ 上下边界 = 表头底部 ~ stop_anchor 上方
→ 按 Y 聚类物理行 → 按 X 分配单元格 → 重建逻辑行 → 输出 items
```

- 列边界：`boundary[i] = (center[i] + center[i+1]) / 2`，不依赖值的对齐方式；
  首/末列**不使用无限边界**，收口为 `first_header.x0 - edge_margin` / `last_header.x1 + edge_margin`
  （`table.edge_margin` 默认 24pt），边缘外词不归入任何列；
- 逻辑行重建三类归属：`prefix continuation`（并入下一明细）、`suffix continuation`
  （并入上一明细 string 列，可 `table.suffix_continuation: false` 关闭）、`standalone noise`
  （记 `table_trailing_rows` 结构问题，不并入）；含"行级合理性守卫"防版式噪声被并成明细；
- 输出 `items`（行关联已建立），回填模板明细字段（`parser="pymupdf-table"` 可审计），
  逐行落 `extracted_items`；
- 跨页续表 `extract_table_multipage`：首页贴底未命中 stop_anchor 时向后续页拼行，复用首页列边界；
- 表格重建失败 → 明细字段回退锚点取值，并记 `table.issues`。

### 7.7 校验层 `pdf/validators/`

| 模块 | 职责 | 结论 |
|---|---|---|
| `field_validator` | `type`（string/decimal/date）+ `pattern` 正则；严重程度 `optional` / `required` / `critical` | critical 失败 → `failed`；required 失败 → `manual_review` |
| `structure_validator` | 表头是否找到、必需列是否齐全、逐行关键单元格是否完整 | `table_structure_inconsistent` → `manual_review` |
| `business_validator` | 逐行 数量×单价≈金额、金额×税率≈税额；Σ明细金额≈合计金额、Σ明细税额≈合计税额；合计金额+税额≈价税合计 | 尾差 → `warning`；明显不成立 → `error`（failed） |
| `base` | `CheckResult`、`money` / `to_decimal` / `to_rate` / `approx_equal` | 金额统一 `Decimal`，`MONEY_TOLERANCE = Decimal("0.02")` 元 |

业务数学是**独立信息源**：双引擎跑同一套锚点规则，锚点指错会一起错；业务规则比"双引擎一致"更接近真正交叉验证。

### 7.8 标准化 `pdf/normalizers.py`

- `normalize_text`：去换行/空格（含全角）、全角数字与冒号括号转半角
- `normalize_amount`：去 `￥/¥/,/元` 与字间距空格 → `Decimal`
- `normalize_date`：`2026年9月9日 / 2026/09/09 / 2026-09-09` → `2026-09-09`

交叉验证比较的是 `normalized_value`，不是原始字符串。

### 7.9 置信度与 Lazy 交叉验证 `pdf/confidence.py`

```
identify_confidence     「选对模板了吗」：指纹总分；fallback 模板固定 60
parse_confidence        「字段取准了吗」：从 100 按风险扣分
validation_confidence   「校验可信吗」：结构 + 业务 + 双引擎独立评分
overall = 识别 30% + 解析 40% + 校验 30%
```

扣分权重：cross_mismatch 20 / structure_issue 15 / business_failure 30 /
required_missing 10 / optional_missing 2 / fallback 模板额外 20~40。
`parse_confidence < PARSE_REVIEW_THRESHOLD(70)` → 转人工复核。

Lazy 触发 `need_secondary_engine`（任一成立即启动 pdfplumber）：
`identify_conf < 85` / `parse_conf < 90` / 结构问题 / 业务问题 / 必填缺失 / critical 失败 / 表格重建失败。
`cross_verify=None` 自动、`True` 强制、`False` 跳过；触发与否、基础解析分、命中数均入审计。

### 7.10 整页文本体检 `pdf/text_audit.py`

1. 无文本层检测（扫描件先给"需 OCR"结论，避免逐字段报"未找到锚点"）；
2. 取值存在性：提取值必须能在原文文本中找到（抓"两引擎一致地取错"与"拼接产物不在原文"），
   合成值字段（`below_mode=count`）除外；
3. 文本相似度（PyMuPDF vs pdfplumber）**仅记录不作判定**。

全部为 advisory：写入原因/审计，不单独改变文档状态。

---

## 8. 模板体系（三层配置）

| 层 | 文件 | 定义内容 |
|---|---|---|
| Base Schema | `templates/base/invoice.json` | 「有哪些字段」：类型/正则/严重程度 + 公共 `regions` / `table` / `structure` / `business_check` / `business_rules` |
| Template | `templates/invoice_v1.json` / `invoice_v2.json` | 「怎么找」：`mode` / `anchor` / `direction` / `region` / `below_mode` / `verify`…，通过 `extends: invoice` 继承 base |
| Variant | 模板内 `variants` | 「哪些字段必须有」：`detect.must` 全命中 → 切换必填集合（如建筑服务版式必填 `construction_site` / `project_name`） |
| Fallback | `templates/invoice_generic.json` | 通用兜底：`fallback=true` + `fallback_detect: ["发票"]` 弱关键词守卫；命中则识别分降档并进人工复核 |

合并规则：字段**深合并**（模板覆盖 base，只写定位键时继承类型/正则/严重程度）；
`regions` / `table` / `structure` / `business_check` / `business_rules` 模板未配置才继承；
`identify` 时返回深拷贝，不污染缓存。

解析模式：`mode` 缺省 `fixed`，支持 `fixed` / `dynamic`；`dynamic_fallback` 缺省 `true`。

---

## 9. 状态机与人工复核

```
pending → processing → …
needs_ocr      路由状态（非业务失败）：无文本层，待 OCR 接入后重新解析
failed         critical 字段失败 / 关键业务关系不成立 / OCR 失败
manual_review  fallback 模板 / 必填失败 / 结构异常 / 双引擎不一致 / 解析置信度 < 70
warning        业务尾差等警告级
success        以上都没有
```

`services/review_service.py` 是人工确认的**单一事实来源**：

- `SUSPECT_STATUSES = ("manual_review", "warning")` 可被确认；`only_suspect=True` 默认只放行可疑项；
- `confirm_document`：`status → success` + 清空 `error_reason` + 未确认校验记录置
  `review_status="confirmed"` / `reviewed_value=primary_value` / `reviewed_at` + 写 `manual_confirm` 审计；
- `confirm_documents` 支持批量、`dry_run` 预演（UI 先弹"将确认 N 条、跳过 M 条"）；
- 筛选页「一键确认」与详情弹窗「确认无误」共用该服务，落库语义完全一致。

---

## 10. 数据层

### 10.1 表结构（`models/document.py`，SQLAlchemy 2.0 Declarative）

| 表 | 关键列 |
|---|---|
| `documents` | id, file_name, file_path, **file_hash(unique,index)**, template_id, template_version, status(index), error_reason, identify_confidence, parse_confidence, overall_confidence, imported_at, created_at, updated_at |
| `extracted_fields` | document_id(FK), field_name, raw_value, normalized_value, parser（`pymupdf` / `pymupdf-table` / `pdfplumber-dynamic`）, confidence |
| `extracted_items` | document_id(FK), row_index, name, spec, unit, quantity, unit_price, amount, tax_rate, tax（Table Engine 逐行明细） |
| `verification_results` | document_id(FK), field_name, primary_value, secondary_value, matched, review_status, reviewed_value, reviewed_at |
| `templates` | template_id(unique), name, version, config_json |
| `audit_logs` | document_id(FK,nullable), action, detail(JSON), created_at |

关系：`Document` 与三个子表均 `cascade="all, delete-orphan"`（覆盖导入时级联清理）。

### 10.2 结构迁移 `database/migrations.py`

- **以模型为唯一事实来源逐表对齐**：缺表建表、缺列补列、缺索引补索引（不再手写补列清单）；
- `PRAGMA user_version` 记录结构版本（`SCHEMA_VERSION = 2`），升级前自动备份 `app.db.bak-v<旧版本>`；
- 库比程序新 → `SchemaTooNewError` 拒绝打开；库文件损坏 → 隔离为备份并重建空库；
- `PRAGMA quick_check` 每进程只跑一次（`_INTEGRITY_CHECKED`），表/列对齐每次都做（轻量）；
- `upgrade()` 幂等；`MigrationReport` 记录新增表/列/索引、备份路径、是否重建。

版本历史：v1 = 5 张基础表；v2 = `documents` 增列（template_version、error_reason、三个 confidence）
+ 新增 `extracted_items`。

### 10.3 事务与并发

- 一份 PDF 的 `documents + extracted_fields + extracted_items + verification_results + audit_logs`
  在**同一 Session 事务**内提交；异常 `rollback` 后置 `failed` 并抛出；
- `get_engine` 设置 `connect_args={"timeout": 30}`，后台解析线程与 UI 线程并发写库时等锁而非立刻
  抛 `database is locked`；
- 去重：`file_sha256` 分块计算，`file_hash` 唯一索引；重复导入抛错，`force=True` 覆盖。

---

## 11. UI 层

### 11.1 主窗口与页面

`MainWindow`：左侧 `Sidebar`（220px 深色，4 个导航项）+ 右侧 `QStackedWidget`，默认 1280×820（最小 960×640）。

| 页面 | 关键能力 |
|---|---|
| `upload_page` | 拖拽/点选 PDF → 入队动画（模拟上传进度）→ `QThread` + `queue.Queue` 后台**串行**解析（避免并发写 SQLite）→ 状态徽章流转：上传中→解析中→已完成/解析失败/待人工确认；`database_changed` 信号驱动筛选页刷新；`shutdown()` 由主窗口在 `exec()` 后与 `closeEvent` 双保险调用 |
| `filter_page` | 从库加载文档列表（18 个数据列，主字段缺失回退旧合同字段）；多关键词 AND 过滤（金额自动兼容 ￥/¥）、状态过滤（含"可疑/待校验"聚合）、开票年月过滤；全选/批量导出 `.xlsx`（openpyxl，按可见列、金额列格式化、冻结表头）、批量删除（写审计）、一键确认、查看详情、整行复制、点击复制单元格；隐藏列与自适应列宽持久化；操作列右侧冻结独立表格 |
| `detail_dialog` | `QtPdf` (`QPdfDocument` + `QPdfView`) 预览，Ctrl+滚轮缩放/拖拽平移；右侧字段列表 + 明细行 + 交叉验证结果 + 三维置信度；「确认无误」走 `review_service.confirm_document`，`document_confirmed` 信号刷新筛选页 |
| `settings_page` | 主题（跟随系统/浅色/深色，切换即时生效）、置信度预警阈值(0~100，默认 85)、`dynamic_fallback` 开关；`QSettings` INI 持久化 |
| `pdf_editor_page` | `QGraphicsView` 自绘预览（scene 坐标 1 单位 = 1pt，点击定位/框选精确映射 PDF 坐标）；工具按钮：浏览/平移、添加文字、修改文字、涂黑遮盖；独立面板：添加水印（当前页 / 全部页）；页面操作：旋转、删除当前页、插入空白页；撤销栈（上限 30）；**内存增量编辑，另存为新文件，原文件不变** |

### 11.2 公共组件与视觉体系

- `ui/widgets/common.py`：`Toast`（右下角自动消失提示，单实例复用）、`Badge`、
  `CopyCellDelegate`（点击复制）、`BadgeDelegate`（表格内圆角状态徽章）、
  `ProgressDelegate`（行内 6px 圆角进度条）
- `ui/widgets/shell.py`：深色侧边栏 `Sidebar`，`page_selected(str)` 信号
- `ui/styles.py`：QSS 样式表 + 颜色 Token（`LIGHT` / `DARK` 两套）、`build_stylesheet(scheme)`、
  `apply_theme(app, choice)`（`system` 时监听 `colorSchemeChanged` 实时切换）、
  `make_app_icon()` 运行时绘制图标、`ui_font(size, weight)`；自绘 delegate 通过 `token(name)`
  运行时取当前主题色。设计稿对应 `tests/ui.html`（蓝色主色 `#2563eb` + slate-900 侧边栏 + 白色卡片）
- `ui/field_labels.py`：字段 key → 中文名唯一映射，被筛选页列定义、导出表头、详情弹窗、
  `services/pdf_service.field_label` 共用，避免"表格中文、弹窗英文"漂移

---

## 12. 路径与部署

### 12.1 运行路径（`paths.py`）

| 用途 | 开发模式（脚本） | 打包模式（exe） |
|---|---|---|
| 只读资源根 `base_dir()` | 项目根 | PyInstaller 解包目录 `<exe>/_internal` |
| 可写数据根 `data_dir()` | `<项目根>/data` | `%APPDATA%\PdfDataTool` |
| SQLite | `data/app.db` | `%APPDATA%\PdfDataTool\app.db` |
| 模板（只读） | `templates/*.json` | `<exe>/_internal/templates/*.json` |
| 界面设置 | `%APPDATA%\PdfDataTool\settings.ini` | 同左 |
| 日志 | `%APPDATA%\PdfDataTool\logs\app.log` | 同左（windowed 无控制台，靠日志排查） |

`ensure_data_dir()` 幂等：建目录 + 打包模式下把旧版 `<exe>/data/` 一次性拷贝到 `%APPDATA%`
（旧目录保留作备份，异常不影响启动）。

### 12.2 打包

- `PdfDataTool.spec`：`datas=[('templates','templates')]`，
  `hiddenimports=['pdfplumber','pypdfium2','sqlalchemy.dialects.sqlite']`，
  `console=False`（windowed），`COLLECT` 输出 `dist/PdfDataTool/`（onedir）
- `build_exe.bat`：打包前备份 `dist/PdfDataTool/data` → `pyinstaller --clean --noconfirm`
  `--add-data "templates;templates"` `--icon assets/app.ico` → 打包后还原用户数据
- 选 `onedir` 而非 `onefile`：Qt 依赖多，启动更快、易排错、不需反复释放临时文件；
  整个 `dist/PdfDataTool` 目录即绿色软件

### 12.3 启动期健壮性（`ui/bootstrap.py`）

- `setup_logging()`：日志落盘失败不阻断启动；`force=True` 重置 handlers，重复调用幂等
- `install_excepthook()`：未捕获异常写 `critical` 日志后再交回原 hook
- `bootstrap_database()`：能自愈的（缺列/缺表/库损坏）由 `ensure_schema` 内部处理；
  处理不了的（版本过新、被占用、无权限）弹 `QMessageBox` 给出**数据库路径 + 处理建议**，
  无 Qt 环境（CLI）则打印 stderr

---

## 13. 测试与诊断

```powershell
# 全量测试
.venv/Scripts/python.exe -m pytest tests/ -q

# 单文件诊断：文档信息 / 双引擎整页文本 / 词元坐标 / 字段级解析对照 / 相似度
.venv/Scripts/python.exe -X utf8 scripts/parse_pdf.py <真票.pdf> [--pages 1] [--area 200-600] [--template invoice_v2]

# UI 冒烟（离屏）
$env:QT_QPA_PLATFORM="offscreen"; .venv/Scripts/python.exe -X utf8 scripts/ui_smoke.py

# 界面截图
.venv/Scripts/python.exe scripts/ui_screenshot.py
```

测试模块（16 个）覆盖：

| 领域 | 模块 |
|---|---|
| 解析引擎 | `test_fixed_parser` / `test_dynamic_parser` / `test_table_engine` / `test_invoice_multiline` / `test_invoice_variant_columns` |
| 模板识别 | `test_template_fingerprint` / `test_generic_fallback` |
| 校验与置信度 | `test_quality_pipeline` / `test_text_audit` / `test_lazy_cross_validation` / `test_error_reasons` |
| 端到端 | `test_pipeline` |
| 数据层 | `test_migrations` |
| 服务层 | `test_review_service` |
| UI | `test_filter_page` / `test_field_labels`（`conftest.py` 统一设置 `QT_QPA_PLATFORM=offscreen`） |

`tests/conftest.py` 以**内联 demo 模板** `contract_v1`（fixed + rect + anchor + business_rules）为固定版式样本，
不依赖生产 `templates/` 目录；`ui.html` 为设计稿基线。

---

## 14. 关键设计原则

1. **解析与判定分离**：Parser 只回答"值是什么"，Validator 回答"值是否合理"，Service 只做编排与状态判定。
2. **一切结论可解释**：置信度是纯扣分规则（无 ML），每处扣分/命中/跳过都写审计 JSON，便于复现与排查。
3. **Region 是搜索空间而非过滤器**：锚点全页查找、取值受区域约束，兼顾"标签缺失不整体失效"与"不串区取错值"。
4. **表格优先**：明细走整表重建得到行关联 `items`，不再逐列找锚点拼行。
5. **贵操作按需触发**：pdfplumber 只在有风险时启动（Lazy Cross Validation），普通 PDF 一次解析直通。
6. **口径统一**：三类引擎共用 `words.py`；PyMuPDF 与 pdfplumber 共用同一套跨页取值策略；
   字段中文名只有 `ui/field_labels.py` 一份来源。
7. **宁缺勿错**：表格首末列收口、边缘外词不归列、suffix 只并入 string 列、结构残余记问题而不硬并。
8. **可升级不丢数据**：模型驱动的结构对齐 + 自动备份 + 版本过新拒绝 + 损坏隔离重建；
   用户数据与程序目录彻底解耦。

---

## 15. 扩展点与当前缺口

| 方向 | 现状 | 接入点 |
|---|---|---|
| OCR（扫描件） | 未接入；无文本层 → `needs_ocr` 路由状态挂起 | `pdf/precheck.py` 的结论替换为"渲染 + OCR → 统一 `Word` 结构"后重入 Region/Anchor/Table/Validation 链路；失败才 `failed` |
| 新发票版式 | 已有 v1（建筑服务数电票）/ v2（增值税专用发票）/ generic 兜底 | 新增 `templates/invoice_vN.json`（`extends: invoice` + `identify` + `fields` 定位键），必要时加 `variants` |
| Repository 层 | `repositories/` 为空，查询在服务/页面层直写 SQLAlchemy | 可把 `filter_page.reload` / 导出 / 删除的查询下沉到 Repository |
| 数据库选型 | SQLite 单机 | SQLAlchemy 抽象已在，切换 PostgreSQL 只需换 URL 与迁移实现 |
| 门禁指标 | 文本相似度仅记录 | `pdf/text_audit.py` 可按需升级为判定项 |
| 依赖精简 | `alembic` / `loguru` / `pydantic` 列在 requirements 但未使用 | 清理或落地（Pydantic 强化模板校验） |
