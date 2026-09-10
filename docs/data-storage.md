# 数据存储位置说明

本文档说明 PdfDataTool 各类数据在**开发模式**与**打包运行（exe）**下的实际存放位置，
以及旧版本数据的一次性迁移机制。

## 1. 总览

| 数据类别 | 开发模式（脚本运行） | 打包运行（exe） |
|---|---|---|
| SQLite 数据库（解析记录） | `<项目根>\data\app.db` | `%APPDATA%\PdfDataTool\app.db` |
| PDF 模板（只读） | `<项目根>\templates\*.json` | `<exe目录>\_internal\templates\*.json` |
| 界面设置（主题/阈值/隐藏列等） | `%APPDATA%\PdfDataTool\settings.ini` | `%APPDATA%\PdfDataTool\settings.ini` |
| 文件日志 | 无（未落盘） | 无（未落盘） |

相关代码：`paths.py`（路径基准）、`ui/pages/settings_page.py`（QSettings）。

## 2. SQLite 数据库

### 2.1 开发模式

```
d:\python-project\pdf-project\data\app.db
```

跟随项目目录，删除整个 `data\` 即可完全重置开发数据。

### 2.2 打包运行（exe）

```
C:\Users\<用户名>\AppData\Roaming\PdfDataTool\app.db
```

即 `%APPDATA%\PdfDataTool\`。该目录与程序安装位置**完全解耦**：

- 用新版 exe / 新版文件夹覆盖更新 → 数据零影响
- 直接删除程序文件夹 → 数据仍在，需要彻底清理时手动删除上述目录
- 换机迁移 → 拷贝 `%APPDATA%\PdfDataTool\` 整个目录

### 2.3 旧版数据自动迁移（一次性）

旧版本把数据库放在 exe 同目录的 `data\` 下。新版 exe 首次启动时
（`paths.ensure_data_dir()`，幂等）自动执行：

```
<exe同目录>\data\app.db 存在 且 %APPDATA%\PdfDataTool\app.db 不存在
    → 将整个旧 data\ 目录拷贝到 %APPDATA%\PdfDataTool\
    → 旧目录原样保留（相当于备份），不会删除
```

- 迁移只发生一次；此后读写均走 `%APPDATA%`
- 迁移失败不影响程序启动（异常被吞掉，新位置照常工作）

## 3. 界面设置（QSettings，INI 文件）

通过 Qt `QSettings`（**INI 格式**，`ui/pages/settings_page.py::load_settings()`）持久化，存储于：

```
%APPDATA%\PdfDataTool\settings.ini
```

即 `C:\Users\<用户名>\AppData\Roaming\PdfDataTool\settings.ini`，与数据库同目录。

包含：置信度预警阈值、动态兜底开关、界面主题、表格隐藏列等。

- 重置界面设置：删除 `settings.ini` 即可
- 彻底清理全部用户数据：删除整个 `%APPDATA%\PdfDataTool\` 目录（含数据库）

> 历史说明：早期版本曾用 Windows 注册表（`HKEY_CURRENT_USER\Software\pdf-project`）
> 存储设置，且不会自动迁移到 INI（相关项将回到默认值）。如注册表有残留，可执行：
>
> ```powershell
> Remove-Item -Path "HKCU:\Software\pdf-project" -Recurse -Force
> ```
>
> 该命令仅删除本应用的注册表键，不影响系统与其他软件。

## 4. PDF 模板（随包分发，只读）

- 打包后位于 `dist\PdfDataTool\_internal\templates\`，由 PyInstaller
  `--add-data "templates;templates"` 带入，随包只读分发
- 更新模板 = 更新程序包；单独改模板 JSON 时，可将新 json 替换到对方
  `_internal\templates\` 下

## 5. 打包与分发的注意事项

1. **开发数据不会进包**：PyInstaller 只收集代码依赖与 `templates\`，
   项目根 `data\` 不会被带入 dist
2. **dist 内可能出现 `data\`**：那是本机运行 dist exe 时产生的（旧版行为残留），
   以整文件夹方式分发前建议先删除：
   ```powershell
   Remove-Item -Recurse -Force .\dist\PdfDataTool\data
   ```
3. 打包脚本 `build_exe.bat` 会在打包前自动备份 / 打包后自动还原本机
   `dist\PdfDataTool\data`，仅用于保护本机测试数据，与 dev 数据无关
