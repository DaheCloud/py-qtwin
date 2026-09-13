# PDF 发票识别混合解析架构（Hybrid Layout Parsing）

> 本文档是当前代码库的架构基线，与 `pdf/`、`templates/`、`services/pdf_service.py` 的实现一一对应。演进历史见 `fixed_pdf_exe_tech_stack.md`。

## 目录

1. [目标与核心思想](#1-目标与核心思想)
2. [总体架构与执行流程](#2-总体架构与执行流程)
3. [目录结构](#3-目录结构)
4. [三层配置：Schema / Template / Variant](#4-三层配置schema--template--variant)
5. [文档预检查 Precheck](#5-文档预检查-precheck)
6. [模板指纹 Fingerprint](#6-模板指纹-fingerprint)
7. [普通字段：Anchor Engine + Region](#7-普通字段anchor-engine--region)
8. [明细表格：Table Engine（Table First）](#8-明细表格table-enginetable-first)
9. [校验层 Validation](#9-校验层-validation)
10. [状态机与置信度](#10-状态机与置信度)
11. [Lazy Cross Validation](#11-lazy-cross-validation)
12. [与旧方案对比](#12-与旧方案对比)
13. [验证与测试](#13-验证与测试)

---

## 1. 目标与核心思想

发票识别存在两类结构完全不同的数据：

- **标签 + 值**字段：发票号码、开票日期、购买方/销售方名称、价税合计。适合「锚点定位 + 相对方向取值」。
- **表格型**数据：商品名称、规格型号、单位、数量、单价、金额、税率、税额。若继续用「每列单独找锚点 + 向下取值」，会逐渐出现错行、跨列、多行单元格拼接困难、表头切词、多条明细关联错误。

因此核心思想是 **混合解析（Hybrid Layout Parsing）**：

- 普通字段：**Anchor First**
- 表格字段：**Table First**
- 所有字段的取值范围：**Region 限定**
- 解析结果的裁判：**Validation**（字段 / 结构 / 业务数学）
- 结果的可信度量：**Confidence**（识别 / 解析 / 校验三维）
- 风险兜底：**Lazy Cross Validation**（必要时才启动第二引擎）

---

## 2. 总体架构与执行流程

```
                PDF
                 │
                 ▼
            Precheck 预检查
       （损坏 / 文本层 / 页数 / 页面尺寸）
                 │
        ┌────────┴─────────────┐
        ▼                      ▼
   needs_ocr（路由状态）     Template Fingerprint 模板指纹识别
   无文本层 ≠ 业务失败       （文本30% + 锚点位置30% + 表头结构30% + 页面特征10%）
        │                      │
        ▼                      ▼
   OCR（接入后）            Variant 变体判定
   成功 → 继续解析       （detect → required_fields 切换必填集合）
   失败 → failed              │
                              ▼
                        Region Engine 页面分区
                              │
                     ┌────────┴────────┐
                     ▼                 ▼
               Anchor Engine      Table Engine
                （普通字段）        （商品明细 → items）
                     │                 │
                     └────────┬────────┘
                              ▼
                         Normalizer 标准化
                              ▼
                      Field Validation 字段校验
                              ▼
                    Structure Validation 结构校验
                              ▼
                   Business Validation 业务数学校验（逐行 + Σ明细≈合计）
                              │
                              ▼
                         Confidence 三维置信度
                              │
                      ┌───────┴───────┐
                      ▼               ▼
                    高可信           低可信
                      │               │
                      ▼               ▼
                   success      pdfplumber（Lazy 交叉验证）
                                       │
                                  ┌────┴────┐
                                  ▼         ▼
                              success   manual_review
```

**无文本层**（扫描件）是**路由条件**而非业务失败原因（方案 §30）：`needs_ocr` 是独立状态（挂起等待 OCR）。OCR 接入后：`needs_ocr` → OCR → 成功则统一为 `Word` 结构重新进入 Region / Anchor / Table / Validation 链路；OCR 失败才 `failed`（失败原因记 OCR 失败，而非"无文本层"）。

---

## 3. 目录结构

```
pdf/
├─ words.py                 # 统一词元层：Word + 行聚类/拼接/切词工具
├─ precheck.py              # 预检查：损坏/文本层/页数/页面尺寸/OCR 触发条件
├─ region_engine.py         # Region 页面分区（header/items/totals）
├─ table_engine.py          # Table Engine：表头→列边界→物理行→逻辑行→items
├─ dynamic_parser.py        # Anchor Engine：锚点 + 相对方向（复用 words）
├─ pymupdf_parser.py        # 固定区域解析 + ParseReport（含 items/table）
├─ pdfplumber_validator.py  # 第二引擎交叉验证（复用同一套锚点/区域规则）
├─ template_engine.py       # 指纹识别 + Base Schema 合并 + Variant 判定
├─ confidence.py            # 识别/解析/校验三维 + overall + Lazy 触发判定
├─ normalizers.py           # 金额/百分比/日期/文本标准化
├─ text_audit.py            # 无文本层 + 取值存在性 + 文本一致性（advisory）
└─ validators/
   ├─ base.py               # CheckResult + Decimal 容差工具
   ├─ field_validator.py    # type/pattern + required/critical/optional
   ├─ structure_validator.py# 表格结构一致性（表格/锚点双模式）
   └─ business_validator.py # 逐行 + Σ明细 + 价税合计（Decimal）

templates/
├─ base/invoice.json        # Base Schema：字段基座 + 公共 regions/table/校验
├─ invoice_v1.json          # 建筑服务数电票（extends + fingerprint + variant）
├─ invoice_v2.json          # 增值税专用发票版式
└─ invoice_generic.json     # 通用兜底

services/pdf_service.py     # 编排 + 状态机 + Lazy 交叉验证 + 落库
models/document.py          # Document / ExtractedField / ExtractedItem / Verification
ui/pages/detail_dialog.py   # 明细行展示 + 三维置信度
scripts/parse_pdf.py        # 诊断脚本（指纹/表格重建/字段对照）
```

---

## 4. 三层配置：Schema / Template / Variant

职责分离（避免模板重复定义字段）：

- **Base Schema** 定义「有哪些字段」——类型 / 正则 / 严重程度，以及各版式共用的 `regions` / `table` / 校验配置。
- **Template** 定义「怎么找」——`anchor` / `direction` / `region` / `anchor_span` / `below_mode` / `scope` 等定位键。
- **Variant** 定义「哪些字段必须有」——按文档特征切换必填集合。

### 4.1 Base Schema（`templates/base/invoice.json`）

```json
{
  "schema": "invoice",
  "regions": {
    "header": {"start_y": 0, "end_anchor": ["项目名称", "货物或应税劳务", "服务名称"]},
    "items":  {"start_anchor": ["项目名称", "货物或应税劳务", "服务名称"],
               "end_anchor": ["合 计", "合计", "价税合计"]},
    "totals": {"start_anchor": ["合 计", "合计"]}
  },
  "table": { "columns": { "name": {"headers": [...]}, "amount": {"type": "decimal"}, ... } },
  "fields": {
    "invoice_no":    {"type": "string", "critical": true, "pattern": "^\\d{8,20}$"},
    "amount":        {"type": "decimal", "critical": true, "pattern": "^[¥￥]?[\\d,]+\\.\\d{2}$"},
    "spec_model":    {"type": "string", "optional": true},
    ...
  }
}
```

### 4.2 Template（`extends` 深合并）

```json
{
  "template": "invoice_v1",
  "mode": "dynamic",
  "extends": "invoice",
  "fields": {
    "invoice_no": {"page": 0, "region": "header", "anchor": "发票号码", "direction": "right", "verify": true},
    "item_name":  {"page": 0, "region": "items", "anchor": "项目名称", "direction": "below", "pattern": "^\\*.+\\*.+$"}
  }
}
```

合并规则：字段深合并（模板覆盖 base，只写定位键时继承类型/正则/严重程度）；`regions` / `table` / `structure` / `business_check` / `business_rules` 模板未配置才继承。

### 4.3 Variant

```json
{
  "variants": {
    "construction": {
      "detect": {"must": ["建筑服务发生地", "建筑项目名称"]},
      "required_fields": ["construction_site", "project_name"]
    }
  }
}
```

`detect.must` 全命中即切换为 `construction` 变体：`required_fields` 移除 `optional` 标记、`optional_fields` 强制 `optional`。识别时 `TemplateEngine.identify` 返回深拷贝应用后的模板，不污染缓存；显式传入的模板在 `service` 层经 `TemplateEngine.prepare` 走同一套合并/变体逻辑。

---

## 5. 文档预检查 Precheck

`pdf/precheck.py` 输出 `PrecheckResult`：

- `ok`：PDF 是否可打开（损坏检测）
- `page_count` / `page_width` / `page_height`：页数与页面尺寸（供指纹识别用）
- `has_text` / `char_count`：文本层是否存在
- `needs_ocr`：完全无文本 → 疑似扫描件（**路由条件**，方案 §30）

语义约定：**无文本层 ≠ failed**。`needs_ocr` 是路由条件（独立状态挂起）：
OCR 接入后由 `needs_ocr → OCR → 成功继续解析 / OCR 失败才 failed`。
`service` 对损坏走 `failed` 快速落库；扫描件落 `needs_ocr` 状态，不进入解析链路；
页面尺寸等特征同时进入审计日志。

---

## 6. 模板指纹 Fingerprint

识别信息从「关键词命中」升级为四类信号加权（0~100 分，方案 §18）：

| 信号 | 权重 | 说明 |
|---|---|---|
| `text` | 30% | `must`（全命中才参评）/ `any` / `exclude` |
| `anchors` | 30% | 文本 + `area` 位置（`top_right` / `left` / `lower_left` …） |
| `table_headers` | 30% | 表头结构（忽略字间距空格差异） |
| `page_size` | 10% | 页面尺寸匹配（容差 ±2pt） |

```json
"identify": {
  "priority": 100,
  "text": {"must": ["建筑服务发生地", "建筑项目名称"], "any": ["电子发票"]},
  "anchors": [
    {"text": "发票号码", "area": "top_right"},
    {"text": "建筑服务发生地", "area": "left"}
  ],
  "table_headers": ["项目名称", "数量", "金额", "税率/征收率", "税额"],
  "page_size": [595.32, 841.92],
  "min_score": 60
}
```

- 分数归一化：只配置了部分信号的模板，按已配置权重归一化（纯文本模板满分仍 100）。
- 老写法（`must/any/exclude/min_score`、`detect`、`fallback_detect`）完全兼容，评分公式不变。
- 位置特征的价值（方案 §19）：多个模板都含「电子发票 / 发票号码 / 金额」时，`发票号码位于右上角` 等位置特征能稳定区分版式。

---

## 7. 普通字段：Anchor Engine + Region

解析过程从「整页找锚点」升级为（Region 是**主要搜索空间**，不是单纯的 Candidate Filter）：

```
整页 words → 找到 Region（y 区间）
  ① Region 内找 anchor + Region 内取值        ← 主路径
  ② Region 内无锚点 → 锚点回退全页查找，
     候选仍限制在 Region 内                     ← 锚点兜底
```

- 两级尝试：Region 内锚点与候选都命中即返回；否则锚点从全页词表查找（表头行常
  同时放着区域标签与字段锚点），候选仍限定在 Region 内——Region 外的同名锚点
  右侧/下方干扰值绝不会被取到。
- 区域标签缺失自动退回全页，规则不因缺标签整体失效。
- 保留并新增的能力：

| 键 | 说明 |
|---|---|
| `region` | 页面区域（`header` / `items` / `totals`），候选值限定 |
| `scope` | `{start_anchor, end_anchor}` 更细的取值分区 |
| `anchor` | 字符串或候选列表（按序尝试，首个成功返回） |
| `direction` | `right` = 同行右侧；`below` = 下方 |
| `anchor_span` | 锚点 x 窗口向右延伸到本列右边界（表头字间距拆词时吸收碎片） |
| `below_mode` | `prefix` / `merge`（跨行单元格）/ `each` / `row`（数值多段拼接）/ `count`（行数统计） |
| `stop_anchor` | 截断关键词（排除合计行 / 信息块） |
| `pick` | `first` / `last`（合计行在明细下方时取列最下方值） |
| `x_min` / `x_max` | 候选 x 范围（区分购方/销方分栏） |

候选按「与锚点的行/列重叠质量」分层：严格同行/同列优先，擦边候选兜底，避免「税」这类多命中锚点借用相邻列候选。

---

## 8. 明细表格：Table Engine（Table First）

`pdf/table_engine.py` 的核心流程（方案 §6-§15）：

```
找表头（候选表头 + 字间距拆词兜底）
→ 生成列边界（相邻表头中心点取中，不依赖值对齐方式）
→ 确定上下边界（表头底部 ~ stop_anchor）
→ 按 Y 聚类物理行
→ 按 X 分配单元格
→ 逻辑行重建（跨行单元格合并）
→ 输出 items
```

### 8.1 列边界（方案 §9）

不继续「anchor → below」，而是相邻表头中心点取中：

```python
# name 中心 78 / quantity 中心 239 → 边界 158.5
boundary[i] = (center[i] + center[i+1]) / 2
```

首列/末列**不使用无限边界**（真实 PDF 中表格左右可能存在备注、序号、页边文本，
无限边界会把它们全部吸进首列/末列），而是收口为：

```python
table_left  = first_header.x0 - edge_margin   # 模板可配 table.edge_margin，默认 24pt
table_right = last_header.x1  + edge_margin
```

边缘外的词不归入任何列（宁缺勿错）。

### 8.2 跨页续表（明细表在第 1 页未结束、合计行在第 2 页）

表格与字段均支持跨页（单页发票行为不变）：

- **表格**：`extract_table_multipage` —— 首页正常重建；首页表格贴到页底
  （未命中 stop_anchor）时向后续页拼行（后续页复用首页列边界，行号接续），
  直到某页命中合计锚点或拼不出明细行。
- **字段**：`extract_field_cross_page`（PyMuPDF 解析器与 pdfplumber 交叉验证
  **共用同一策略**，保证双引擎口径一致）：
  1. 配置页优先，失败按页序兜底（字段可能被排到后续页，如备注区信息块）；
  2. 带 region 的字段在某页 region 无法解析（区域锚标签不在本页）时**跳过
     该页**——该页取值不受 region 约束、不可信（否则 pick=last 会取到明细
     行错值并挡住后续页）；
  3. 全部单页失败后，用"配置页 + 后续页"的扩展画布（`merged_page_words`，
     页偏移拼接 + 页间距）再试一次——锚点在配置页（如"金额"表头）、值在
     后续页（合计行在第 2 页）只有画布能同时看到两者。
- **指纹**：文本特征取前两页（建筑服务信息块等可能落在第 2 页），位置指纹
  只看首页版式。
- **取值存在性审计**：覆盖全部文档页，避免跨页取值被误报。

### 8.3 逻辑行重建（方案 §12-§14）

非数据行（只有名称/规格/单位，无主数据列）的三类归属：

| 类别 | 条件 | 归属 |
|---|---|---|
| prefix continuation | 名称行**之后跟着数据行** | 并入下一条明细（单元格折行的常态） |
| suffix continuation | 表尾名称行**后面没有数据行** | 并入上一条明细的 string 列（"商品A …" 下方的附加说明属于上一行，而不是下一条商品）；模板可用 `table.suffix_continuation: false` 关闭 |
| standalone noise | suffix 归属后仍未消化的残余（数值列残余等） | 记 `table_trailing_rows` 结构问题，不并入 |

- **行级合理性守卫**：主数据列出现明显不是数值的长文本（表格下方信息块说明文字
  落入数值列）不算数据行，防止版式噪声被并成明细；
- suffix 只并入 string 列（名称/规格/单位），数值列残余不并入，避免污染数量/金额。

### 8.3 输出（方案 §15）

不返回互相独立的列数组，而是行关联已建立的 `items`：

```python
items = [
    {"row_index": 1, "name": "建筑服务某某道路工程第三阶段",
     "spec": "", "unit": "项", "quantity": "2", "unit_price": "100.00",
     "amount": "200.00", "tax_rate": "9%", "tax": "18.00"},
]
```

- 明细字段回填模板（`parser="pymupdf-table"` 可审计），行数/各列行数由 items 统计；
- `extracted_items` 表逐行落库，详情弹窗「商品明细」区块展示；
- 表格重建失败时明细字段回退锚点取值，结构问题记入 `table.issues`。

---

## 9. 校验层 Validation

Parser 只负责「我认为字段值是 XXX」，Validator 才负责「XXX 是否合理」。

| 校验 | 职责 | 结论 |
|---|---|---|
| Field Validator | `type` / `pattern` / `required` / `critical` | critical 失败 → `failed`；required 失败 → `manual_review` |
| Structure Validator | 表头是否找到、必需列是否齐全、逐行关键单元格是否完整 | `table_structure_inconsistent` → `manual_review` |
| Business Validator | 逐行数量×单价≈金额、金额×税率≈税额；Σ明细金额≈合计金额、Σ明细税额≈合计税额；合计金额+税额≈价税合计 | 尾差 → `warning`；明显不成立 → `error`(failed) |

- 金额全部用 `Decimal`，容差 `MONEY_TOLERANCE = Decimal("0.02")` 元。
- 业务数学是独立信息源：双引擎一致只能证明「切词一致」，锚点指错地方时两边会一起错——业务规则比单纯双引擎一致更接近真正的交叉验证（方案 §25）。
- 结构校验不再使用「item_rows > 1 → 人工」，改用「表格结构是否自洽」。

---

## 10. 状态机与置信度

### 10.1 状态机

```
needs_ocr     路由状态（非业务失败）：无文本层，等待 OCR 后重新解析
failed        critical 字段失败 / 关键业务关系不成立 / OCR 失败（接入后）
manual_review fallback 模板 / 必填字段失败 / 结构异常 / 双引擎不一致 / 置信度过低
success       以上都没有
```

### 10.2 置信度（三维 + 综合，方案 §31/§32）

| 维度 | 说明 |
|---|---|
| `identify_confidence` | 「选对模板了吗」——指纹总分；fallback 固定 60 |
| `parse_confidence` | 「字段取准了吗」——字段/结构/业务/交叉按风险扣分 |
| `validation_confidence` | 「校验可信吗」——结构 + 业务 + 双引擎独立评分 |

综合：`overall_confidence = 识别 30% + 解析 40% + 校验 30%`。三维与综合均落库并在详情弹窗展示。

---

## 11. Lazy Cross Validation

不建议所有 PDF 默认都跑 PyMuPDF + pdfplumber（方案 §26-§28）。

```
PyMuPDF 主解析 → Validation → Confidence
  ├─ 高可信 → 直接 success
  └─ 有风险 → 启动 pdfplumber（解决争议）
```

触发条件（`need_secondary_engine`）：

```python
identify_conf < 85
or parse_conf < 90
or structure_issues
or business_issues
or required_missing
or critical_failed
or table_failed      # 表格重建失败
```

`PdfService.process_document(..., cross_verify=None)`：`None` = 按风险自动；`True` = 强制；`False` = 跳过。触发与否、基础解析分、命中数均写入审计日志。普通 PDF 一次解析直通，CPU 更低、职责更清晰。

---

## 12. 与旧方案对比

| 旧 | 新 |
|---|---|
| Anchor 是核心 | Region + Table + Anchor |
| 每个明细字段单独找 | 整表重建 → items |
| 多行主要靠字段 merge | Logical Row 逻辑行 |
| 模板只看文本关键词 | 文本 + 位置 + 表头指纹（0~100） |
| 所有字段全页搜索 | Region 限定候选值 |
| item_rows > 1 → 人工 | 表格结构一致性 → 人工 |
| 双引擎固定执行 | 风险触发（Lazy） |
| 模板重复定义字段 | Base Schema + Template + Variant |
| 多行明细只取首行 | 逐行 items 全提取 + 逐行校验 |
| OCR 独立处理 | OCR 输出统一 Words 后复用后续链路 |

---

## 13. 验证与测试

```powershell
# 全量测试（157 项）
.venv/Scripts/python.exe -m pytest tests/ -q

# 单文件诊断：指纹评分、表格重建（列边界 + 逐行 items）、字段对照
.venv/Scripts/python.exe -X utf8 scripts/parse_pdf.py <真票.pdf>

# UI 冒烟
QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -X utf8 scripts/ui_smoke.py
```

测试覆盖要点：

- `tests/test_table_engine.py`：表头定位（含字间距拆词）、列边界、逻辑行合并、items、字段回填、逐行/Σ 校验
- `tests/test_template_fingerprint.py`：指纹打分、area 位置语义、老写法兼容、extends 合并、Variant
- `tests/test_lazy_cross_validation.py`：Lazy 触发策略、precheck、三维置信度
- `tests/test_invoice_multiline.py` / `test_invoice_variant_columns.py`：多行明细逐行提取、合计字段稳定命中
