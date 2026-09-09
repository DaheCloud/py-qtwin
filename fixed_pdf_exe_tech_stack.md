# 固定结构 PDF 识别 EXE 技术栈方案

> Python + PySide6 + PyMuPDF + pdfplumber + SQLite

适用场景：PDF 本身包含可选择文本。系统默认使用 **固定区域模式作为主解析方式**；当模板明确配置为非固定区域，或固定区域解析失败 / 字段校验异常时，再使用非固定区域解析策略。最终将关键字段提取、校验后写入本地数据库，并打包为 Windows EXE。

---

## 1. 推荐技术栈

| 模块 | 推荐技术 | 职责 | 备注 |
|---|---|---|---|
| 桌面 UI | PySide6 / Qt 6 | 窗口、表格、PDF 预览、进度、设置页 | 适合原生 Windows 桌面应用 |
| PDF 主解析 | PyMuPDF | 文本、坐标、页面、固定区域提取 | 速度快，作为主解析器 |
| PDF 二次验证 | pdfplumber | 关键字段、字符坐标、表格/布局辅助解析 | 用于交叉验证，不建议全文重复解析 |
| 数据库 | SQLite | 保存 PDF、字段、校验结果、历史记录 | 单机 EXE 无需额外安装数据库 |
| ORM | SQLAlchemy 2 | 数据库模型与访问层 | 方便后续切换 PostgreSQL 等 |
| 数据库迁移 | Alembic | 表结构版本升级 | 长期维护推荐 |
| 配置 | JSON / Pydantic | PDF 模板、坐标、字段规则 | 避免把坐标写死在业务代码里 |
| 日志 | Loguru / logging | 错误与解析日志 | 便于定位异常 PDF |
| 打包 | PyInstaller 或 Nuitka | 生成 Windows EXE | 先用 PyInstaller，性能/保护需求高再考虑 Nuitka |

---

## 2. 总体架构

```text
PySide6 UI
   ↓
PDF 文件导入
   ↓
模板识别 / 模板选择
   ↓
PyMuPDF 固定区域提取（主结果）
   ↓
字段清洗与格式化
   ↓
字段规则验证
   ↓
关键字段由 pdfplumber 二次提取
   ↓
交叉验证 / 一致性判断
   ↓
异常字段人工确认
   ↓
SQLAlchemy
   ↓
SQLite
```

---

## 2.1 默认解析策略

系统默认采用：

```text
固定区域模式 = 主解析
```

也就是：

```text
PDF
 ↓
模板匹配
 ↓
固定页码 + 固定坐标区域
 ↓
PyMuPDF 主解析
 ↓
字段清洗
 ↓
字段规则验证
```

只有以下情况才进入非固定区域解析：

```text
1. 模板明确配置 mode = dynamic
2. 固定区域没有提取到有效文本
3. 字段格式校验失败
4. 字段值明显异常
5. 页面布局发生偏移，固定坐标无法稳定命中
```

推荐整体策略：

```text
                 PDF
                  ↓
            模板识别 / 选择
                  ↓
        默认：固定区域主解析
                  ↓
          字段是否有效？
             /      \
           是        否
           ↓         ↓
        继续      非固定区域解析
           \         /
            ↓       ↓
            字段标准化
                  ↓
              规则校验
                  ↓
        关键字段 pdfplumber 验证
                  ↓
               SQLite
```

这样可以保证：

- 大部分固定模板 PDF 使用最快、最稳定的路径。
- 非固定区域解析只作为特殊模板或异常兜底。
- 不会让所有 PDF 都走更复杂的动态定位逻辑。
- 后续排查问题时也更容易判断是坐标模板问题还是动态解析问题。

---

## 3. 为什么固定结构 PDF 不需要 OCR

- PDF 可以用鼠标选中文字，说明文件通常已经存在文本层。
- 直接读取文本层通常比 OCR 更快、更准确，也不会产生 OCR 常见的错字。
- 固定结构 PDF 的核心不是“识别整页”，而是“确定字段所在区域并稳定提取”。
- 只有遇到扫描件、图片 PDF 或文本层损坏时，才考虑 PaddleOCR / Tesseract 作为兜底。

---

## 4. 固定模板解析策略

建议为每一种 PDF 版式建立独立模板配置。

字段至少包含：

- 页码
- 矩形区域
- 字段类型
- 校验规则
- 是否需要二次验证

例如：

```json
{
  "template": "contract_v1",
  "mode": "fixed",
  "page_size": [595, 842],
  "fields": {
    "contract_no": {
      "page": 0,
      "rect": [80, 100, 250, 130],
      "type": "string",
      "pattern": "^HT\\d{8}$",
      "verify": true
    },
    "customer_name": {
      "page": 0,
      "rect": [80, 150, 350, 180],
      "type": "string"
    },
    "amount": {
      "page": 0,
      "rect": [400, 300, 550, 330],
      "type": "decimal",
      "verify": true
    }
  }
}
```

后续如果 PDF 模板发生变化：

```text
contract_v1
contract_v2
invoice_v1
invoice_v2
```

只需要新增模板配置，不需要大量修改解析代码。

---

## 5. PyMuPDF 主解析

固定结构 PDF 最适合按照页面坐标进行字段提取。

例如：

```python
import fitz

doc = fitz.open("sample.pdf")
page = doc[0]

rect = fitz.Rect(80, 100, 250, 130)
contract_no = page.get_textbox(rect).strip()

print(contract_no)
```

如果页面尺寸和结构固定，可以直接通过矩形坐标读取字段。

这种方式通常比：

```text
全文提取
↓
查找关键字
↓
猜测字段位置
```

更稳定。

推荐流程：

```text
固定页码
  ↓
固定坐标区域
  ↓
提取文本
  ↓
字段清洗
```

---

## 6. 字段验证

字段提取完成后，建议增加多层验证。

### 6.1 格式验证

例如合同编号：

```python
import re

if not re.fullmatch(r"HT\d{8}", contract_no):
    print("合同编号格式异常")
```

日期：

```text
YYYY-MM-DD
```

金额：

```text
12800.00
```

手机号、编号、统一社会信用代码等，也可以通过正则进行验证。

---

### 6.2 业务规则验证

除了格式，还可以验证业务逻辑。

例如：

```text
金额 >= 0
```

```text
开始日期 <= 结束日期
```

```text
合同编号不能重复
```

```text
客户名称不能为空
```

这样可以进一步减少错误数据进入数据库。

---

## 7. pdfplumber 二次交叉验证

不建议：

```text
PyMuPDF 全文解析
+
pdfplumber 全文解析
```

所有 PDF 都重复两遍。

更推荐：

```text
PyMuPDF
   ↓
主解析全部字段
   ↓
筛选关键字段
   ↓
pdfplumber 二次解析
   ↓
比较结果
```

关键字段例如：

- 合同编号
- 订单编号
- 金额
- 日期
- 姓名
- 客户名称
- 身份标识
- 银行账号

示例：

| 字段 | PyMuPDF | pdfplumber | 结果 |
|---|---|---|---|
| 合同编号 | HT20260001 | HT20260001 | 通过 |
| 金额 | 12800.00 | 12800.00 | 通过 |
| 客户名称 | ABC公司 | ABC有限公司 | 需人工确认 |

最终判断逻辑：

```text
两个解析结果一致
    ↓
自动通过

两个结果不一致
    ↓
标记异常
    ↓
人工确认
```

---

## 8. 字段标准化

两个解析器返回的数据可能存在：

```text
12,800.00
￥12,800
12800.00
```

因此比较之前应该先标准化。

例如：

```python
from decimal import Decimal

def normalize_amount(value: str) -> Decimal:
    value = (
        value
        .replace("￥", "")
        .replace("¥", "")
        .replace(",", "")
        .strip()
    )

    return Decimal(value)
```

字符串也可以统一处理：

```python
def normalize_text(value: str) -> str:
    return (
        value
        .replace("\n", "")
        .replace(" ", "")
        .strip()
    )
```

因此交叉验证最好比较：

```text
normalized_value
```

而不是直接比较原始字符串。

---

## 9. 推荐数据库设计

### documents

PDF 文件主表。

核心字段：

```text
id
file_name
file_path
file_hash
template_id
template_version
status
imported_at
created_at
updated_at
```

---

### extracted_fields

字段解析结果。

核心字段：

```text
id
document_id
field_name
raw_value
normalized_value
parser
confidence
created_at
```

例如：

```text
parser = pymupdf
```

---

### verification_results

交叉验证结果。

核心字段：

```text
id
document_id
field_name
primary_value
secondary_value
matched
review_status
reviewed_value
reviewed_at
```

---

### templates

PDF 模板信息。

```text
id
template_id
name
version
config_json
created_at
```

---

### audit_logs

操作日志。

```text
id
document_id
action
detail
created_at
```

---

## 10. 推荐项目目录

```text
project/
├─ main.py
│
├─ ui/
│  ├─ main_window.py
│  ├─ pages/
│  │  ├─ home.py
│  │  ├─ import_page.py
│  │  ├─ review_page.py
│  │  └─ settings.py
│  └─ widgets/
│
├─ pdf/
│  ├─ pymupdf_parser.py
│  ├─ pdfplumber_validator.py
│  ├─ template_engine.py
│  ├─ normalizers.py
│  └─ validators.py
│
├─ templates/
│  ├─ contract_v1.json
│  ├─ contract_v2.json
│  └─ invoice_v1.json
│
├─ models/
│  ├─ document.py
│  ├─ extracted_field.py
│  └─ verification.py
│
├─ repositories/
│  ├─ document_repository.py
│  └─ field_repository.py
│
├─ services/
│  ├─ pdf_service.py
│  ├─ verification_service.py
│  └─ import_service.py
│
├─ database/
│  ├─ db.py
│  └─ migrations/
│
├─ assets/
│  ├─ icons/
│  └─ images/
│
├─ config/
│
└─ logs/
```

---

## 11. PySide6 界面建议

推荐主界面：

```text
┌─────────────────────────────────────────────────────┐
│ 打开 PDF   批量导入   开始解析   导出   设置         │
├───────────────────────┬─────────────────────────────┤
│                       │ 字段名称      结果      状态 │
│                       │                             │
│      PDF 预览         │ 合同编号    HT001      ✓   │
│                       │ 客户名称    ABC公司     ✓   │
│                       │ 金额        12800      ✓   │
│                       │ 日期        2026...     ⚠   │
│                       │                             │
├───────────────────────┴─────────────────────────────┤
│ 当前文件：xxx.pdf              解析进度：85%        │
└─────────────────────────────────────────────────────┘
```

建议：

- 左侧显示 PDF。
- 右侧显示字段提取结果。
- 正常字段显示通过。
- 异常字段突出显示。
- 双击字段可以人工修改。
- 修改后保存人工确认结果。

---

## 12. PDF 预览

PySide6 可以直接使用 Qt PDF：

```python
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
```

可以实现：

```text
PDF 翻页
缩放
适应窗口
滚动
页面导航
```

如果后续需要显示字段位置：

```text
合同编号
金额
日期
```

还可以在 PDF 页面上叠加矩形框：

```text
PDF
↓
字段坐标
↓
高亮区域
```

让用户知道程序究竟读取了哪里。

---

## 13. 模板识别

如果系统中只有一种 PDF：

```text
直接使用 contract_v1
```

即可。

如果未来存在多个模板，可以通过：

```text
第一页固定标题
公司名称
关键字段
页数
页面尺寸
```

进行模板识别。

例如：

```python
def detect_template(text: str):
    if "销售合同" in text:
        return "contract_v1"

    if "采购订单" in text:
        return "purchase_order_v1"

    return None
```

最终：

```text
PDF
 ↓
模板检测
 ↓
对应 JSON
 ↓
固定区域解析
```

---

## 14. 文件去重

导入 PDF 时建议计算 SHA-256。

```python
import hashlib

def file_sha256(path):
    sha256 = hashlib.sha256()

    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            sha256.update(chunk)

    return sha256.hexdigest()
```

数据库记录：

```text
file_hash
```

如果已经存在：

```text
该 PDF 已经导入
```

避免重复数据。

---

## 15. 批量处理

如果以后一次导入：

```text
100 个 PDF
500 个 PDF
1000 个 PDF
```

不要直接在 PySide6 UI 主线程解析。

应该：

```text
UI 主线程
   ↓
任务队列
   ↓
Worker
   ↓
PDF Parser
```

避免界面卡死。

PySide6 可以使用：

```text
QThread
QThreadPool
QRunnable
```

---

## 16. 异常处理机制

推荐状态：

```text
pending
processing
success
warning
failed
manual_review
```

例如：

```text
PDF
 ↓
PyMuPDF 提取
 ↓
格式验证失败
 ↓
pdfplumber 再解析
 ↓
仍然不一致
 ↓
manual_review
```

人工确认后：

```text
manual_review
 ↓
confirmed
```

---

## 17. SQLite + SQLAlchemy

推荐通过 SQLAlchemy 管理 SQLite。

```python
from sqlalchemy import create_engine

engine = create_engine(
    "sqlite:///data/app.db"
)
```

不要在业务代码中到处直接使用：

```python
sqlite3.connect(...)
```

推荐：

```text
UI
 ↓
Service
 ↓
Repository
 ↓
SQLAlchemy
 ↓
SQLite
```

这样未来切换：

```text
PostgreSQL
MySQL
服务端 API
```

会更容易。

---

## 18. 数据库事务

一份 PDF 的处理建议放到事务中。

例如：

```text
documents
extracted_fields
verification_results
```

全部写入成功：

```text
COMMIT
```

任何一步出错：

```text
ROLLBACK
```

避免出现：

```text
PDF 主表存在
但是字段只有一半
```

这种脏数据。

---

## 19. 日志

建议记录：

```text
PDF 文件名
模板
字段
原始值
清洗值
解析器
异常原因
处理时间
```

例如：

```text
2026-09-09 10:31:25
file=contract001.pdf
field=amount
pymupdf=12800
pdfplumber=12300
status=mismatch
```

开发阶段可以使用：

```text
Loguru
```

或者 Python 自带：

```text
logging
```

---

## 20. EXE 打包

开发阶段推荐：

```text
PyInstaller
```

例如：

```bash
pyinstaller main.py ^
  --name PdfDataTool ^
  --windowed ^
  --onedir
```

第一阶段建议使用：

```text
--onedir
```

而不是：

```text
--onefile
```

因为 PySide6 + Qt 的依赖较多，`onedir`：

- 更容易排错
- 启动更快
- 不需要每次释放大量临时文件

等程序稳定之后，再考虑：

```text
onefile
```

或者：

```text
Nuitka
```

---

## 21. MVP 开发顺序

### 第一阶段

完成：

```text
PySide6
+
打开 PDF
+
PDF 预览
```

### 第二阶段

完成：

```text
PyMuPDF
+
一个固定模板
+
固定坐标字段提取
```

### 第三阶段

加入：

```text
字段清洗
字段格式验证
```

### 第四阶段

加入：

```text
SQLite
+
SQLAlchemy
```

保存 PDF 和解析结果。

### 第五阶段

加入：

```text
pdfplumber
```

针对关键字段进行交叉验证。

### 第六阶段

增加：

```text
异常字段人工确认
```

### 第七阶段

增加：

```text
多模板 JSON
模板版本
模板自动识别
```

### 第八阶段

增加：

```text
批量 PDF
日志
Excel / CSV 导出
```

### 第九阶段

完成：

```text
PyInstaller / Nuitka
Windows EXE
```

---

## 22. 最终推荐方案

核心技术栈：

```text
Python
+
PySide6 / Qt 6
+
PyMuPDF
+
pdfplumber
+
SQLAlchemy 2
+
SQLite
+
JSON / Pydantic
+
Alembic
+
PyInstaller
```

核心设计原则：

```text
固定模板
   ↓
固定坐标
   ↓
PyMuPDF 主解析
   ↓
字段标准化
   ↓
格式 / 业务规则校验
   ↓
关键字段 pdfplumber 二次验证
   ↓
异常人工确认
   ↓
SQLite
```

对于固定结构、可以直接选择文字的 PDF，这种方案通常比：

```text
OCR
AI 全文识别
多个 PDF 引擎全文重复解析
```

更加：

- 快
- 准
- 稳定
- 容易维护
- 容易排查错误
- 适合 Windows EXE 单机软件


---

## 23. 双模式解析设计

系统支持两种解析模式，但默认优先使用固定区域模式。

### 23.1 模式 A：固定区域模式（默认 / 主解析）

适合：

```text
页面尺寸固定
字段位置固定
表格位置固定
模板版本明确
```

主流程：

```text
page + rect
   ↓
PyMuPDF get_textbox()
   ↓
字段清洗
   ↓
字段验证
```

模板示例：

```json
{
  "template": "contract_v1",
  "mode": "fixed",
  "fields": {
    "contract_no": {
      "page": 0,
      "rect": [80, 100, 250, 130],
      "type": "string",
      "pattern": "^HT\\d{8}$",
      "verify": true
    }
  }
}
```

这是系统的默认模式。

如果模板中不写 `mode`，也可以约定：

```text
mode 缺省 = fixed
```

例如：

```python
mode = template.get("mode", "fixed")
```

---

### 23.2 模式 B：非固定区域模式

非固定区域模式主要用于：

```text
字段上下位置会变化
某些字段长度变化导致布局偏移
同一模板存在轻微版式差异
字段有稳定关键词，但坐标不稳定
```

推荐通过 PyMuPDF 的：

```python
page.get_text("words")
```

或：

```python
page.get_text("blocks")
```

获取文字和坐标。

然后使用：

```text
关键词锚点
+
相对方向
+
距离范围
+
正则规则
```

寻找字段。

例如 PDF：

```text
合同编号：HT20260001
```

解析规则：

```json
{
  "contract_no": {
    "anchor": "合同编号",
    "direction": "right",
    "same_line": true,
    "type": "string",
    "pattern": "^HT\\d{8}$"
  }
}
```

解析过程：

```text
找到“合同编号”
      ↓
获取 anchor 坐标
      ↓
寻找同行右侧最近文本
      ↓
HT20260001
```

---

### 23.3 非固定区域常用规则

推荐实现以下规则：

#### 同行右侧

```text
客户名称：ABC有限公司
```

```json
{
  "anchor": "客户名称",
  "direction": "right",
  "same_line": true
}
```

#### 下方文本

```text
客户名称
ABC有限公司
```

```json
{
  "anchor": "客户名称",
  "direction": "below",
  "max_distance": 50
}
```

#### 正则定位

例如合同号：

```python
r"HT\d{8}"
```

#### 表格标题定位

例如：

```text
商品名称 | 数量 | 单价 | 金额
```

先定位表头，再根据列坐标解析数据。

---

## 24. 推荐解析优先级

建议解析引擎统一按照下面的顺序执行：

```text
PDF
 ↓
模板识别
 ↓
mode 是否为 dynamic？
   /         \
 否           是
 ↓            ↓
Fixed       Dynamic
 ↓            ↓
PyMuPDF    PyMuPDF words/blocks
Rect       Anchor / Relative Layout
 ↓            ↓
 └──────┬─────┘
        ↓
   字段标准化
        ↓
   字段规则验证
        ↓
   是否验证成功？
      /      \
    是        否
    ↓         ↓
  正常     尝试动态兜底
              ↓
          仍然失败
              ↓
          人工确认
```

对于固定模板，推荐：

```text
固定区域主解析
+
非固定区域异常兜底
+
pdfplumber 关键字段交叉验证
```

而不是：

```text
固定区域
+
动态区域
+
pdfplumber
```

每次全部执行。

这样性能更好，逻辑也更清晰。

---

## 25. 模板模式约定

建议模板配置统一采用：

```json
{
  "template": "contract_v1",
  "mode": "fixed"
}
```

支持：

```text
fixed
dynamic
```

默认值：

```text
fixed
```

代码：

```python
mode = template.get("mode", "fixed")

if mode == "fixed":
    result = fixed_parser.parse(pdf, template)
else:
    result = dynamic_parser.parse(pdf, template)
```

固定区域失败后可以根据配置决定是否兜底：

```json
{
  "template": "contract_v1",
  "mode": "fixed",
  "dynamic_fallback": true
}
```

对应：

```python
result = fixed_parser.parse(pdf, template)

if not result.valid and template.get("dynamic_fallback", True):
    result = dynamic_parser.parse(pdf, template)
```

最终推荐默认：

```text
mode = fixed
dynamic_fallback = true
```

也就是：

**固定区域主解析，非固定区域作为兜底。**
