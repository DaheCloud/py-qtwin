@echo off
rem 固定结构 PDF 识别工具 - 一键打包为 Windows EXE（PyInstaller onedir）
rem 产物：dist\PdfDataTool\PdfDataTool.exe（整个 PdfDataTool 目录即绿色软件）
cd /d "%~dp0"
set PYTHONUTF8=1

rem 1) 确保打包工具可用
".venv\Scripts\python.exe" -m pip show pyinstaller >nul 2>nul
if errorlevel 1 (
    echo [build] installing pyinstaller ...
    ".venv\Scripts\python.exe" -m pip install pyinstaller
)

rem 2) 打包：windowed（无控制台）+ onedir；templates/ 模板随包分发
".venv\Scripts\pyinstaller.exe" main.py ^
  --name PdfDataTool ^
  --windowed ^
  --onedir ^
  --clean ^
  --noconfirm ^
  --add-data "templates;templates" ^
  --hidden-import pdfplumber ^
  --hidden-import pypdfium2 ^
  --hidden-import sqlalchemy.dialects.sqlite

if errorlevel 1 (
    echo [build] FAILED
    exit /b 1
)

echo.
echo [build] OK -^> dist\PdfDataTool\PdfDataTool.exe
echo [build] 整个 dist\PdfDataTool 目录即可分发；首次运行会在同目录创建 data\app.db
