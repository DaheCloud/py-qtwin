# PdfDataTool 解析核心 V2 优化方案

> 基于当前 `PdfService.process_document`、模板体系、Anchor/Table Engine、双引擎校验、置信度与状态机进行增量重构。  
> 目标不是推倒重写，而是在保留现有解析能力的基础上，降低“程序判定成功但实际取值错误”的概率，并提升可维护性、可解释性与后续 OCR/新票据类型扩展能力。

---

## 1. 改造目标

当前解析链路已经具备：

- PDF 预检查
- 模板指纹识别
- Fixed / Dynamic 两套字段解析
- Table First 表格解析
- PyMuPDF + pdfplumber 双引擎
- 结构校验
- 业务数学校验
- 三维置信度
- 人工复核
- AuditLog 审计

这些能力继续保留。

V2 重点解决以下问题：

1. `dynamic_fallback=true` 默认开启，可能产生静默误解析。
2. Region 失败直接退回全页，关键字段可能从错误区域取值。
3. PyMuPDF 与 pdfplumber 仍使用同一套 Anchor 规则，不属于完全独立的交叉验证。
4. 当前置信度是固定扣分模型，可解释性较弱。
5. Template 同时承担识别、定位、策略、校验等过多职责。
6. Table Engine 主要依赖表头中心点推导列边界，对非标准对齐表格不够稳。
7. `documents.status` 同时表达处理状态、质量状态和人工复核状态。
8. 人工确认直接将状态改为 `success`，会丢失“机器判断”和“人工确认”之间的区别。

核心目标：

```text
降低：

“解析成功，但值其实错了”

这种假成功 / 静默错误。
```

---

# 2. 总体架构调整

## 2.1 当前模型

```text
PDF
↓
Precheck
↓
Template Identify
↓
Fixed / Dynamic Parser
↓
Table Parser
↓
Validation
↓
Cross Verify
↓
Confidence
↓
Status
```

## 2.2 V2 推荐模型

```text
PDF
↓
Precheck
↓
Unified Words
├─ PyMuPDF
├─ pdfplumber
└─ OCR（未来）
↓
Layout Detection
↓
┌──────────────────────────────┐
│                              │
Field Engine               Table Engine
│                              │
└──────────────┬───────────────┘
               ↓
        Candidate / Evidence
               ↓
        Evidence Fusion
        ├─ Geometry
        ├─ Text
        ├─ Pattern
        ├─ Cross Engine
        ├─ Semantic
        └─ Business
               ↓
        Field Confidence
               ↓
        Document Quality
               ↓
┌──────────────┼───────────────┐
success      review          reject
```

核心变化：

```text
V1：
解析出一个值 → 校验这个值

V2：
解析出候选值 → 收集证据 → 多证据判断这个值是否可信
```

---

# 3. 修改一：Dynamic Fallback 改成字段级 Opt-in

## 3.1 当前问题

当前：

```json
{
  "dynamic_fallback": true
}
```

通常作为模板默认值。

这意味着：

```text
Fixed 失败
↓
Dynamic Anchor 自动救援
↓
找到一个格式合法的值
↓
可能继续被系统接受
```

对于普通字段问题不大。

但以下字段风险较高：

- invoice_no
- amount
- tax
- total_amount
- total_tax
- amount_with_tax
- seller_tax_no
- buyer_tax_no

因为这些字段一旦取错，可能仍然满足：

- 正则正确
- decimal 转换成功
- 两个 PDF 引擎都能找到

最终形成静默错误。

---

## 3.2 推荐修改

模板默认：

```json
{
  "dynamic_fallback": false
}
```

字段需要时单独开启：

```json
{
  "fields": {
    "invoice_date": {
      "anchor": "开票日期",
      "direction": "right",
      "dynamic_fallback": true
    }
  }
}
```

关键字段推荐：

```json
{
  "fields": {
    "invoice_no": {
      "critical": true,
      "dynamic_fallback": false
    },
    "amount_with_tax": {
      "critical": true,
      "dynamic_fallback": false
    }
  }
}
```

---

## 3.3 推荐新增 fallback_policy

比单纯 boolean 更灵活：

```json
{
  "fallback_policy": "none"
}
```

支持：

```text
none
anchor
table
page
document
```

例如：

```json
{
  "fields": {
    "invoice_no": {
      "fallback_policy": "none"
    },
    "remark": {
      "fallback_policy": "document"
    },
    "invoice_date": {
      "fallback_policy": "anchor"
    }
  }
}
```

---

# 4. 修改二：Region Failure 不再统一回退全页

## 4.1 当前问题

当前区域解析失败时：

```text
region 标签不存在
↓
退回整页 words
```

例如：

```text
totals.amount
```

理论上只应该从：

```text
totals region
```

查找。

如果退回整页：

```text
明细金额
合计金额
价税合计
其他金额

都可能成为候选。
```

---

## 4.2 推荐增加 region_failure_policy

模板级默认：

```json
{
  "region_failure_policy": "fail"
}
```

字段可覆盖：

```json
{
  "fields": {
    "remark": {
      "region_failure_policy": "page"
    }
  }
}
```

支持：

```text
fail
page
document
```

推荐策略：

| 字段级别 | Region 失败策略 |
|---|---|
| critical | fail |
| required | page |
| optional | document |

---

## 4.3 Region 失败要生成证据

新增：

```python
RegionEvidence(
    expected_region="totals",
    region_found=False,
    fallback_used=False
)
```

如果发生 fallback：

```python
RegionEvidence(
    expected_region="header",
    region_found=False,
    fallback_used=True,
    fallback_scope="page"
)
```

后续进入 Field Confidence。

---

# 5. 修改三：从“双引擎校验”升级为“多证据验证”

## 5.1 当前问题

当前：

```text
PyMuPDF
+
pdfplumber
```

都使用相同：

```text
region
anchor
direction
scope
stop_anchor
pick
```

所以它们实际上验证的是：

```text
不同文本提取器
是否在同一套规则下得到相同结果
```

而不是：

```text
这个业务字段是否真的正确
```

例如 Anchor 配错：

```text
PyMuPDF     → 12800
pdfplumber  → 12800
```

两边一致。

但正确值可能是：

```text
18200
```

---

## 5.2 V2 Evidence 结构

每个字段生成：

```python
FieldEvidence
```

建议：

```python
@dataclass
class FieldEvidence:
    field_name: str

    value: str | None

    anchor_score: float | None
    geometry_score: float | None
    pattern_score: float | None
    region_score: float | None

    text_engine_score: float | None
    business_score: float | None
    semantic_score: float | None

    fallback_penalty: float = 0
    ambiguity_penalty: float = 0

    evidence: list = field(default_factory=list)
```

---

## 5.3 Evidence 来源

### A. Anchor Evidence

```text
锚点是否找到
锚点是不是完整命中
候选是否位于预期方向
距离是否合理
```

例如：

```json
{
  "anchor": 1.0,
  "direction": 1.0,
  "distance": 0.95
}
```

---

### B. Geometry Evidence

例如发票号码：

```text
预期：右上角
实际：右上角
```

得分高。

如果：

```text
预期：右上角
实际：页面底部
```

即使正则正确，也降低置信度。

---

### C. Pattern Evidence

例如：

```text
发票号码：8~20 位数字
税率：0% ~ 100%
金额：合法 Decimal
日期：合法日期
```

---

### D. Cross Engine Evidence

```text
PyMuPDF == pdfplumber
```

得分：

```text
1.0
```

不同：

```text
0.0 ~ 0.5
```

但 Cross Engine 不再作为“最终正确”的证明，只是证据之一。

---

### E. Business Evidence

例如：

```text
数量 × 单价 ≈ 金额
金额 × 税率 ≈ 税额

Σ 明细金额 ≈ 合计金额
Σ 明细税额 ≈ 合计税额

金额 + 税额 ≈ 价税合计
```

这是比“双文本引擎一致”更独立的证据。

---

### F. Semantic Evidence

后续可以加入：

```text
seller_name 周围是否出现“销售方”
buyer_name 周围是否出现“购买方”

total_amount 附近是否出现“合计”
```

第一阶段可以仍然采用规则实现，不需要 AI。

---

# 6. 修改四：置信度从“扣分制”改成 Evidence Score

## 6.1 当前问题

当前类似：

```text
parse_confidence = 100

cross mismatch      -20
structure issue     -15
business failure    -30
required missing    -10
```

这种方式容易出现：

```text
82 分到底代表什么？
```

不好解释。

---

## 6.2 改成字段级评分

例如：

```text
invoice_no

anchor        0.98
geometry      0.95
pattern       1.00
region        1.00
cross_engine  1.00
business      N/A

最终：
0.98
```

金额：

```text
amount

anchor        0.95
geometry      0.98
pattern       1.00
cross_engine  1.00
business      0.40

最终：
0.76
```

系统可以明确告诉 UI：

```text
金额字段低置信度原因：

业务金额关系不一致
```

---

## 6.3 推荐评分

第一阶段可以简单加权：

```python
field_score = (
    anchor_score * 0.20
    + geometry_score * 0.20
    + pattern_score * 0.15
    + region_score * 0.15
    + cross_engine_score * 0.15
    + business_score * 0.15
)
```

注意：

不存在的证据维度不应该按 0 计算。

应该：

```text
对实际参与的维度重新归一化权重。
```

---

## 6.4 Critical 字段不要用平均值掩盖风险

错误：

```text
invoice_no = 100
date       = 100
seller     = 100
amount     = 30

average = 82.5
```

然后认为整单正常。

推荐：

```python
critical_min = min(critical_field_scores)
```

Document Score 可以：

```python
document_score = (
    critical_min * 0.35
    + required_avg * 0.25
    + table_score * 0.15
    + business_score * 0.15
    + identify_score * 0.10
)
```

并增加硬规则：

```text
critical field < 0.65
→ 不允许 success
```

---

# 7. 修改五：Template 职责拆分

## 7.1 当前问题

一个 Template 目前可能同时包含：

```text
identify
fields
region
anchor
direction
scope
pattern
verify
table
business
fallback
variant
```

模板越来越多后容易演变成复杂 DSL。

---

# 7.2 推荐四层

```text
Schema
Layout
Strategy
Validation
```

---

## 7.3 Schema

负责：

```text
有哪些字段
字段类型
字段业务意义
字段重要程度
```

例如：

```json
{
  "schema": "invoice",
  "fields": {
    "invoice_no": {
      "type": "string",
      "critical": true
    },
    "amount": {
      "type": "decimal",
      "critical": true
    }
  }
}
```

---

## 7.4 Layout

负责：

```text
这个版式字段在哪里
```

例如：

```json
{
  "layout": "invoice_v1",
  "fields": {
    "invoice_no": {
      "region": "header",
      "anchor": "发票号码",
      "direction": "right"
    }
  }
}
```

---

## 7.5 Strategy

负责：

```text
怎么解析
```

例如：

```json
{
  "strategies": {
    "invoice_no": "anchor_right",
    "item_table": "header_columns",
    "amount_with_tax": "anchor_right_last"
  }
}
```

也可以支持：

```text
rect
anchor_right
anchor_below
table_column
regex_near_anchor
```

---

## 7.6 Validation

负责：

```text
是否合法
字段之间关系是否正确
```

例如：

```json
{
  "validation": {
    "invoice_no": {
      "pattern": "^\\d{8,20}$"
    }
  }
}
```

业务规则：

```json
{
  "business": [
    "amount + tax ≈ amount_with_tax"
  ]
}
```

---

# 8. 修改六：Table Engine 使用“初始列 + 数据修正”

## 8.1 当前方式

当前：

```text
Header1 center
Header2 center
↓
中点
↓
Column Boundary
```

优点：

- 实现简单
- 固定模板稳定
- 不依赖数据内容

但以下场景可能偏移：

```text
短表头 + 宽数据列
右对齐数字
金额列特别窄
名称列特别宽
表头换行
```

---

## 8.2 V2 推荐算法

### Step 1

仍然使用：

```text
表头中心
```

计算初始边界。

### Step 2

提取前：

```text
3~5 行
```

物理行。

### Step 3

统计：

```text
每列 word x-center 分布
```

### Step 4

计算：

```text
column cluster
```

### Step 5

只允许在一定范围内修正：

```text
± 8 ~ 15pt
```

避免数据反向污染表头结构。

---

## 8.3 输出 Table Evidence

例如：

```json
{
  "header_score": 1.0,
  "column_alignment_score": 0.92,
  "row_consistency_score": 0.95,
  "required_columns_score": 1.0
}
```

形成：

```text
table_score
```

---

# 9. 修改七：状态机拆成三个维度

## 9.1 当前

```text
pending
processing
needs_ocr
failed
manual_review
warning
success
```

这里实际上混合了：

```text
处理状态
质量状态
人工复核状态
```

---

## 9.2 V2

### Processing Status

```text
pending
processing
completed
needs_ocr
error
```

字段：

```python
processing_status
```

---

### Quality Status

```text
valid
warning
invalid
unknown
```

字段：

```python
quality_status
```

---

### Review Status

```text
not_required
pending
confirmed
corrected
rejected
```

字段：

```python
review_status
```

---

## 9.3 示例

自动解析完全正确：

```json
{
  "processing_status": "completed",
  "quality_status": "valid",
  "review_status": "not_required"
}
```

自动解析存在可疑项：

```json
{
  "processing_status": "completed",
  "quality_status": "warning",
  "review_status": "pending"
}
```

人工确认没问题：

```json
{
  "processing_status": "completed",
  "quality_status": "warning",
  "review_status": "confirmed"
}
```

注意：

```text
quality_status 不应该被改成 valid。
```

因为：

```text
机器当时确实认为它存在风险。
```

---

# 10. 修改八：人工确认不再覆盖机器原始结论

## 10.1 当前

人工确认：

```text
manual_review
↓
success
```

这样后续无法区分：

```text
机器自动通过

和

人工确认后通过
```

---

## 10.2 推荐

保留：

```python
quality_status
```

只修改：

```python
review_status
```

例如：

```text
机器：
quality_status = warning
review_status  = pending

人工确认：
quality_status = warning
review_status  = confirmed
```

对 UI 可以映射最终展示状态：

```python
def final_display_status(doc):
    if doc.processing_status == "needs_ocr":
        return "等待 OCR"

    if doc.processing_status == "error":
        return "解析失败"

    if doc.quality_status == "invalid":
        return "数据异常"

    if doc.review_status == "confirmed":
        return "人工已确认"

    if doc.review_status == "pending":
        return "待复核"

    return "准确"
```

---

# 11. 新增 Candidate 概念

这是 V2 比较重要的内部改动。

当前通常是：

```text
一个字段
↓
一个结果
```

V2 推荐：

```text
一个字段
↓
多个 Candidate
↓
Evidence Ranking
↓
Best Candidate
```

例如：

```python
FieldCandidate(
    value="12800",
    source="anchor",
    page=1,
    rect=(...),
    anchor="价税合计",
    score=0.68
)

FieldCandidate(
    value="18200",
    source="table_total",
    page=1,
    rect=(...),
    anchor="合计",
    score=0.96
)
```

最后：

```text
18200
```

胜出。

---

# 12. 推荐的数据模型

## 12.1 ExtractedField

保留现有字段。

增加：

```text
confidence
selected_candidate_id
evidence_json
fallback_used
```

例如：

```json
{
  "field": "amount",
  "raw_value": "18,200.00",
  "normalized_value": "18200.00",
  "confidence": 0.94,
  "fallback_used": false
}
```

---

## 12.2 FieldCandidate

可先只放 Audit，不一定第一版就单独建表。

```text
field_name
value
normalized_value
page
rect
parser
strategy
score
selected
```

---

## 12.3 Evidence

建议第一阶段：

```text
JSON
```

即可。

例如：

```json
{
  "anchor": 0.98,
  "geometry": 0.95,
  "region": 1.0,
  "pattern": 1.0,
  "cross_engine": 1.0,
  "business": 0.92
}
```

以后如果需要统计，再拆表。

---

# 13. 新版解析链路

推荐：

```text
0  SHA256 + 查重

1  Precheck
   ├─ broken
   ├─ text PDF
   └─ needs OCR

2  Template Identify

3  Layout Resolve

4  Unified Words

5  Generate Field Candidates

6  Generate Table Candidates

7  Candidate Normalize

8  Evidence Collection
   ├─ anchor
   ├─ geometry
   ├─ region
   ├─ pattern
   ├─ cross engine
   └─ business

9  Candidate Ranking

10 Select Best Candidate

11 Structure Validation

12 Business Validation

13 Field Confidence

14 Document Quality Score

15 Processing / Quality / Review Status

16 Audit

17 Commit
```

---

# 14. 与现有代码的对应修改

建议不要推倒重写。

## 保留

```text
pdf/words.py
pdf/precheck.py
pdf/template_engine.py
pdf/region_engine.py
pdf/dynamic_parser.py
pdf/table_engine.py
pdf/validators/
pdf/normalizers.py
pdf/text_audit.py
```

---

## 新增

```text
pdf/evidence/
├── __init__.py
├── base.py
├── anchor.py
├── geometry.py
├── region.py
├── pattern.py
├── cross_engine.py
├── business.py
└── scorer.py
```

新增：

```text
pdf/candidates.py
```

建议：

```python
@dataclass
class FieldCandidate:
    field_name: str
    raw_value: str
    normalized_value: Any

    page: int
    rect: tuple | None

    parser: str
    strategy: str

    evidence: dict
    score: float = 0
```

---

# 15. process_document 推荐重构

不要让：

```python
process_document()
```

继续无限增长。

推荐：

```python
def process_document(...):

    context = create_parse_context(...)

    precheck(context)

    identify_template(context)

    extract(context)

    validate(context)

    score(context)

    determine_states(context)

    persist(context)
```

进一步：

```python
extract(context)
```

内部：

```python
extract_fields(context)

extract_table(context)

build_candidates(context)

select_candidates(context)
```

这样 `PdfService` 只负责编排。

---

# 16. ParseContext

建议增加统一上下文。

```python
@dataclass
class ParseContext:

    pdf_path: Path

    template: dict | None = None

    words: dict[int, list[Word]] = field(default_factory=dict)

    fields: dict = field(default_factory=dict)

    candidates: dict = field(default_factory=dict)

    items: list = field(default_factory=list)

    evidence: dict = field(default_factory=dict)

    errors: list = field(default_factory=list)

    warnings: list = field(default_factory=list)

    audit: dict = field(default_factory=dict)
```

优点：

```text
不用 process_document 里传几十个局部变量。
```

---

# 17. 状态判定建议

## 17.1 Processing

```python
if pdf_broken:
    processing_status = "error"

elif needs_ocr:
    processing_status = "needs_ocr"

else:
    processing_status = "completed"
```

---

## 17.2 Quality

```python
if critical_invalid:
    quality_status = "invalid"

elif document_score < 0.65:
    quality_status = "invalid"

elif required_low_confidence:
    quality_status = "warning"

elif document_score < 0.85:
    quality_status = "warning"

else:
    quality_status = "valid"
```

---

## 17.3 Review

```python
if quality_status == "valid":
    review_status = "not_required"

else:
    review_status = "pending"
```

人工确认：

```python
review_status = "confirmed"
```

人工修改：

```python
review_status = "corrected"
```

---

# 18. 推荐阈值

第一版暂时可以：

```text
Field

>= 0.90   High
0.75~0.90 Medium
< 0.75    Low
```

Critical：

```text
< 0.80
→ 强制 review
```

Document：

```text
>= 0.90   valid

0.75~0.90 warning

< 0.75    invalid / review
```

注意：

这些分数第一阶段仍然是：

```text
Evidence Score
```

不要宣传成：

```text
90% 准确率
```

除非以后有真实人工标注数据进行校准。

---

# 19. 审计日志增强

建议 AuditLog 增加：

```json
{
  "selected_candidate": {},
  "other_candidates": [],
  "evidence": {},
  "fallback": {},
  "region": {},
  "cross_engine": {},
  "business": {},
  "field_scores": {},
  "document_score": 0.91
}
```

这样出现错误时可以回答：

```text
为什么系统选择了这个值？
```

而不是只知道：

```text
parser = pymupdf
```

---

# 20. UI 可以获得的能力

详情页可以显示：

```text
金额：18,200.00

可信度：94%

证据：

✓ 合计区域
✓ “金额”锚点定位
✓ PyMuPDF / pdfplumber 一致
✓ Decimal 格式正确
✓ 明细合计匹配
```

如果有问题：

```text
金额：12,800.00

可信度：68%

⚠ Region 未找到
✓ 正则正确
✓ 双引擎一致
✕ 明细合计不匹配
```

人工复核人员马上能知道：

```text
为什么需要复核。
```

---

# 21. 推荐迁移顺序

不要一次全部修改。

## Phase 1：先堵住静默错误

优先修改：

```text
dynamic_fallback
region fallback
critical field fallback
```

目标：

```text
宁可 manual_review
不要假 success
```

这是收益最高的一步。

---

## Phase 2：状态机拆分

增加：

```text
processing_status
quality_status
review_status
```

旧：

```text
status
```

暂时保留做兼容。

---

## Phase 3：Evidence

先实现：

```text
anchor
geometry
pattern
cross_engine
business
```

---

## Phase 4：Field Score

增加：

```text
ExtractedField.confidence
ExtractedField.evidence_json
```

---

## Phase 5：Candidate

从：

```text
first successful result
```

改：

```text
multiple candidates
→ ranking
→ selected candidate
```

---

## Phase 6：Table Boundary Correction

在现有 Table Engine 上增量实现。

---

## Phase 7：OCR

OCR 输出：

```python
Word(x0, y0, x1, y1, text)
```

即可复用：

```text
Region
Anchor
Table
Evidence
Validation
```

---

# 22. 最终推荐目录

```text
pdf/
├── words.py
├── precheck.py
├── template_engine.py
├── region_engine.py
│
├── candidates.py
│
├── parsers/
│   ├── fixed.py
│   ├── anchor.py
│   └── table.py
│
├── evidence/
│   ├── base.py
│   ├── anchor.py
│   ├── geometry.py
│   ├── region.py
│   ├── pattern.py
│   ├── cross_engine.py
│   ├── business.py
│   └── scorer.py
│
├── validators/
│   ├── field_validator.py
│   ├── structure_validator.py
│   └── business_validator.py
│
├── normalizers.py
├── confidence.py
└── text_audit.py
```

---

# 23. 不建议修改的部分

以下设计建议继续保留：

## 统一 Word 层

```python
Word(
    x0,
    y0,
    x1,
    y1,
    text
)
```

这是整个系统最重要的基础抽象之一。

未来：

```text
PyMuPDF
pdfplumber
OCR
```

都可以转换成相同结构。

---

## Table First

继续保留。

原因：

```text
表格字段之间具有天然行关系。
```

比：

```text
每一列独立 Anchor 提取
```

更可靠。

---

## Decimal 业务校验

继续保留。

这是非常重要的独立证据来源。

---

## Base / Template / Variant 思路

继续保留。

只是逐步将：

```text
Schema
Layout
Strategy
Validation
```

职责拆清楚。

---

# 24. 改造后的核心收益

## 24.1 准确性

最大改善：

```text
降低“取到了一个合法但错误的值”的概率。
```

---

## 24.2 可解释性

以前：

```text
parse_confidence = 72
```

以后：

```text
amount = 72

Anchor       95
Geometry     90
Pattern     100
CrossEngine 100
Business     20
```

可以明确知道：

```text
业务关系异常导致低分。
```

---

## 24.3 可维护性

模板从：

```text
一个 JSON 控制全部行为
```

逐步变成：

```text
Schema
+
Layout
+
Strategy
+
Validation
```

---

## 24.4 可扩展性

未来增加：

```text
OCR
新的 PDF 引擎
新的票据类型
新的表格布局
视觉模型
```

都只需要增加 Evidence / Parser。

核心流程不需要推倒重写。

---

# 25. 一句话总结

V1 的核心是：

```text
规则解析
+
解析后校验
```

V2 推荐升级为：

```text
候选提取
+
多证据验证
+
风险决策
```

对于财务、发票、票据类 PDF：

最重要的不是：

```text
尽量提取出一个值
```

而是：

```text
只有足够可信时才自动通过。
```

系统应优先避免：

```text
假成功
```

即：

```text
status = success
```

但实际字段取错。

这也是本次 V2 改造最核心的优化目标。
