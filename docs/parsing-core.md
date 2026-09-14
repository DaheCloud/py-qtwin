# PdfDataTool 核心解析链路与规则体系

> 本文是 `docs/tech-architecture.md` 的**专项抽取**，只保留四部分内容：
> ① `process_document` 16 步链路 ② 解析引擎 10 个模块 ③ 模板三层体系 ④ 状态机与复核服务。
> 技术栈、目录结构、数据层、UI、部署等见 `docs/tech-architecture.md`。

---

## 目录

1. [核心处理链路（`PdfService.process_document`）](#1-核心处理链路pdfserviceprocess_document)
2. [解析引擎 10 个模块详解](#2-解析引擎-10-个模块详解)
3. [模板体系（三层配置）](#3-模板体系三层配置)
4. [状态机与人工复核](#4-状态机与人工复核)

---

## 1. 核心处理链路（`PdfService.process_document`）

编排入口：`services/pdf_service.py::PdfService.process_document(session, pdf_path, template=None, *, force=False, cross_verify=None)`

一次性完成「预检查 → 识别 → 解析 → 三层校验 → 交叉验证 → 置信度 → 状态机 → 事务落库」。

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

### 1.1 各步骤要点补充

| 步 | 要点 |
|---|---|
| 0 | `file_sha256` 分块（1MB）计算；`force=False` 时重复文件直接抛 `ValueError`，避免脏数据 |
| 1 | 预检查结论决定是否进入解析链路；损坏与扫描件都是「快速落库 + 审计」后返回 |
| 2 | 自动识别走指纹打分；显式传入的模板与自动识别**同构**（同样 base 合并 + variant 应用），只是 `mode=manual` |
| 4 | `_parse_with_fallback` 只在「fixed 解析整体无效且 `dynamic_fallback=true`」时才逐字段兜底，救回的字段名记入 `rescued_fields` 并触发 `DYNAMIC_RESCUED` 警告 |
| 6 | 只有 `critical` 进 errors（整单 failed）；`required` 进 warnings（转人工）；`optional` 缺失标 `advisory=True`，不写进用户可见原因 |
| 10 | 双引擎只在"有争议"时启动；固定模板只对 `rect` 字段、动态模板只对 `anchor` 字段做交叉验证 |
| 13 | `parse_confidence` 计算两次：`base_parse_conf` 用于 Lazy 触发判定（不含交叉验证扣分），最终值含交叉验证结果 |
| 16 | 任何异常统一 `rollback` + 置 `failed` 后 `raise`，由 UI 层捕获回填行状态 |

### 1.2 落库内容一览

| 表 | 写入时机 |
|---|---|
| `documents` | 步 3 建记录（processing）→ 步 14 更新 status / 三个置信度 / error_reason |
| `extracted_fields` | 步 5 逐字段（raw / normalized / parser） |
| `extracted_items` | 步 5 逐行明细（Table Engine 输出） |
| `verification_results` | 步 10 交叉验证逐字段（matched 时 `review_status="confirmed"`） |
| `audit_logs` | 预检查快速路径 或 步 15（结构化 JSON） |

---

## 2. 解析引擎 10 个模块详解

模块全在 `pdf/` 下，按调用顺序排列：

### 2.1 统一词元层 `pdf/words.py`

所有取词来源（PyMuPDF 文本层、pdfplumber、未来 OCR）统一为 `Word(x0, y0, x1, y1, text)`（frozen dataclass），
提供行聚类 `group_rows`、同行拼接 `merged_line_words`、相邻拼接 `merged_neighbor_words`、
跨页画布 `merged_page_words`、数值碎片合并 `with_number_fragments`、行数统计 `count_rows` 等。

关键常量：`ANCHOR_MERGE_GAP_RATIO = 1.6`（吸收「单 位」「税 额」字间距拆词）、
`TRAILING_PLACEHOLDERS`（清除单元格占位符「—」）、`NUMBER_FRAGMENT`（金额被切成多段数字）。

> 三类引擎共用同一份 Words 口径，因此"换取词引擎"不会导致规则分叉。

### 2.2 预检查 `pdf/precheck.py`

输出 `PrecheckResult`：`ok / page_count / page_width / page_height / has_text / char_count / error`，
派生属性 `needs_ocr = ok and not has_text`。文本层按**任意页有文本**判定（多页文档首页可能是图片封面），
达标即提前停止扫描。页面尺寸取第 1 页，供模板指纹使用。

### 2.3 模板指纹识别 `pdf/template_engine.py`

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

### 2.4 页面分区 `pdf/region_engine.py`

模板声明 `regions: {header / items / totals}`，`field_words()` 为字段候选词做 y 区间过滤：

- start 边界 = 起始标签行最上沿（含该行，同行值不被误伤，容差 1.0pt）
- end 边界 = 结束标签行最上沿（严格小于 → 结束行被排除）
- 区域标签全部缺失 → 退回全页（规则不因缺标签整体失效）
- **锚点始终从全页词表查找**（表头行常同时承载区域标签与字段锚点），只有"取值候选"受限

### 2.5 锚点引擎 `pdf/dynamic_parser.py`

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

### 2.6 表格引擎 `pdf/table_engine.py`（Table First）

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

### 2.7 校验层 `pdf/validators/`

| 模块 | 职责 | 结论 |
|---|---|---|
| `field_validator` | `type`（string/decimal/date）+ `pattern` 正则；严重程度 `optional` / `required` / `critical` | critical 失败 → `failed`；required 失败 → `manual_review` |
| `structure_validator` | 表头是否找到、必需列是否齐全、逐行关键单元格是否完整 | `table_structure_inconsistent` → `manual_review` |
| `business_validator` | 逐行 数量×单价≈金额、金额×税率≈税额；Σ明细金额≈合计金额、Σ明细税额≈合计税额；合计金额+税额≈价税合计 | 尾差 → `warning`；明显不成立 → `error`（failed） |
| `base` | `CheckResult`、`money` / `to_decimal` / `to_rate` / `approx_equal` | 金额统一 `Decimal`，`MONEY_TOLERANCE = Decimal("0.02")` 元 |

业务数学是**独立信息源**：双引擎跑同一套锚点规则，锚点指错会一起错；业务规则比"双引擎一致"更接近真正交叉验证。

### 2.8 标准化 `pdf/normalizers.py`

- `normalize_text`：去换行/空格（含全角）、全角数字与冒号括号转半角
- `normalize_amount`：去 `￥/¥/,/元` 与字间距空格 → `Decimal`
- `normalize_date`：`2026年9月9日 / 2026/09/09 / 2026-09-09` → `2026-09-09`

交叉验证比较的是 `normalized_value`，不是原始字符串。

### 2.9 置信度与 Lazy 交叉验证 `pdf/confidence.py`

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

第二引擎 `pdf/pdfplumber_validator.py`：`PdfplumberValidator.verify()` 对 `verify=true` 的字段
独立切词（宽松 `x_tolerance=8` 找锚点、保守默认切词取值）后跑同一套锚点规则，比较标准化结果，
不一致进 `ENGINE_MISMATCH`。

### 2.10 整页文本体检 `pdf/text_audit.py`

1. 无文本层检测（扫描件先给"需 OCR"结论，避免逐字段报"未找到锚点"）；
2. 取值存在性：提取值必须能在原文文本中找到（抓"两引擎一致地取错"与"拼接产物不在原文"），
   合成值字段（`below_mode=count`）除外；
3. 文本相似度（PyMuPDF vs pdfplumber）**仅记录不作判定**。

全部为 advisory：写入原因/审计，不单独改变文档状态。

---

## 3. 模板体系（三层配置）

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

### 3.1 三层各自长什么样

Base Schema（`templates/base/invoice.json`，节选）：

```json
{
  "schema": "invoice",
  "regions": {
    "header": {"start_y": 0, "end_anchor": ["项目名称", "货物或应税劳务", "服务名称"]},
    "items":  {"start_anchor": ["项目名称", "货物或应税劳务", "服务名称"],
               "end_anchor": ["合 计", "合计", "价税合计"]},
    "totals": {"start_anchor": ["合 计", "合计"]}
  },
  "table": {
    "columns": {"name": {"headers": ["项目名称"]}, "amount": {"type": "decimal"}},
    "stop_anchor": ["合 计", "合计", "价税合计"],
    "required_columns": ["name", "amount", "tax"]
  },
  "fields": {
    "invoice_no": {"type": "string", "critical": true, "pattern": "^\\d{8,20}$"},
    "amount":     {"type": "decimal", "critical": true}
  }
}
```

Template（`templates/invoice_v1.json`，节选——只写「怎么找」）：

```json
{
  "template": "invoice_v1",
  "mode": "dynamic",
  "extends": "invoice",
  "identify": {
    "priority": 100,
    "text": {"must": ["建筑服务发生地", "建筑项目名称"], "any": ["电子发票"]},
    "anchors": [{"text": "发票号码", "area": "top_right"}],
    "table_headers": ["项目名称", "数量", "金额", "税率/征收率", "税额"],
    "page_size": [595.32, 841.92],
    "min_score": 60
  },
  "fields": {
    "invoice_no": {"region": "header", "anchor": "发票号码", "direction": "right", "verify": true},
    "item_name":  {"region": "items", "anchor": "项目名称", "direction": "below", "pattern": "^\\*.+\\*.+$"}
  }
}
```

Variant（模板内，按文档特征切换必填集合）：

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

`required_fields` 会移除对应字段的 `optional` 标记；`optional_fields` 则强制为 `optional`。

### 3.2 识别与合并的执行顺序

```
TemplateEngine.__init__(templates_dir)
  → reload()：扫描 templates/*.json，load 后 _apply_base() 深合并（extends 解析）
identify(pdf_path)
  → DocFingerprint（首页文本 + 词元坐标 + 页面尺寸）
  → 各模板 evaluate_fingerprint() 打分 → 取最高分且 ≥ min_score
  → 命中专属模板：_apply_variant()（按 must 命中切必填集合）→ 返回深拷贝
  → 全部淘汰：fallback 模板（fallback=true + fallback_detect 守卫）
prepare(template, first_page_text)   # 显式指定模板时走这条，与自动识别同构
```

新增版式只需新增一个 `templates/invoice_vN.json`（`extends: invoice` + `identify` + `fields` 定位键），
不改解析代码。

### 3.3 模板未显式声明时的默认值

| 键 | 默认值 | 影响 |
|---|---|---|
| `mode` | `fixed` | 决定走 `FixedRegionParser` 还是 `DynamicRegionParser` |
| `dynamic_fallback` | `true` | 固定解析失败时是否逐字段锚点兜底 |
| `verify` | 无（不参与） | 未标 `true` 的字段不进交叉验证 |
| 字段严重程度 | `required` | 非 `optional` 且非 `critical` 即 required |
| `table.edge_margin` | 24pt | 表格首末列收口余量 |

---

## 4. 状态机与人工复核

### 4.1 状态机

```
pending → processing → …
needs_ocr      路由状态（非业务失败）：无文本层，待 OCR 接入后重新解析
failed         critical 字段失败 / 关键业务关系不成立 / OCR 失败
manual_review  fallback 模板 / 必填失败 / 结构异常 / 双引擎不一致 / 解析置信度 < 70
warning        业务尾差等警告级
success        以上都没有
```

判定顺序（`determine_status`）：**先判 failed，再判 manual_review，最后才是 success**。

| 输入 | 取值来源 |
|---|---|
| `critical_failed` | errors 中 `CRITICAL_FIELD_FAILED` 计数 |
| `business_errors` | 业务数学 error + 模板 `business_rules` 失败数 |
| `identify_mode` | `match` / `manual` 才可能 success；`fallback` 直接 manual_review |
| `cross_mismatch` | 交叉验证不一致字段数 |
| `structure_issues` | 结构校验失败项数 |
| `business_warnings` | 业务 warning 数 |
| `required_failed` | `REQUIRED_FIELD_FAILED` 计数 |
| `parse_confidence` | 最终解析置信度（< 70 → manual_review） |

主要问题码：`PDF_BROKEN` / `NEEDS_OCR` / `CRITICAL_FIELD_FAILED` / `REQUIRED_FIELD_FAILED` /
`OPTIONAL_MISSING`(advisory) / `BUSINESS_RULE_FAILED` / `ENGINE_MISMATCH` / `DYNAMIC_RESCUED` /
`TABLE_REBUILD_FAILED` / `ITEM_ROWS_MISMATCH` / `VALUE_NOT_IN_TEXT` / `TEMPLATE_FALLBACK`。

### 4.2 人工复核服务 `services/review_service.py`

**单一事实来源**：筛选页「一键确认」与详情弹窗「确认无误」共用这里，保证两条入口落库语义完全一致
（状态、复核记录、审计日志）。

```python
SUSPECT_STATUSES = ("manual_review", "warning")   # 可被确认的状态
```

| 函数 | 行为 |
|---|---|
| `is_suspect(status)` | 是否属于「可疑/待校验」 |
| `confirm_document(session, doc, source)` | 确认单条：`status → success`；清空 `error_reason`；未确认的校验记录置 `review_status="confirmed"` / `reviewed_value=primary_value` / `reviewed_at`；写 `manual_confirm` 审计。返回本次标记条数。**不提交事务** |
| `confirm_documents(session, doc_ids, source, only_suspect=True, dry_run=False)` | 批量确认，返回 `ConfirmReport(confirmed / skipped / missing / confirmed_fields)`；`dry_run=True` 只统计不落库 |

语义约定：

- `only_suspect=True`（默认）只放行可疑状态——已经准确的不必再确认；解析失败的关键字段可能缺失，
  不适合一键放行，会进入 `skipped` 并在提示里说明；
- **只做"标记确认"，不做数值改写**：取值对不对由人工在详情弹窗核对后再确认；
- 调用方负责 `session.commit()`（与项目其它写操作一致）；
- 审计 `detail` 带来源（`source=filter_page` / `manual`），可追溯是哪个入口确认的。

### 4.3 复核与状态的联动

```
上传页：后台解析 → 落库 status
   ↓ database_changed
筛选页：读库列表（状态徽章：准确 / 可疑待校验 / 解析失败 / 等待 OCR）
   ├─ 一键确认（批量）→ 二次弹窗「将确认 N 条、跳过 M 条」→ confirm_documents → 刷新
   └─ 查看详情 → detail_dialog
        ├─ PDF 预览 + 字段/明细/三维置信度
        └─ 确认无误 → confirm_document → document_confirmed 信号 → 筛选页刷新
```

**当前缺口**：`needs_ocr` 为挂起态，无 OCR 引擎，只能等待人工处理；OCR 接入后
应走「`needs_ocr` → OCR → 统一 `Word` 结构 → 重入 Region / Anchor / Table / Validation 链路」，
OCR 失败才落 `failed`（原因记 OCR 失败，而非"无文本层"）。
