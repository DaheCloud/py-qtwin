@echo off
rem 固定结构 PDF 识别工具 - 一键打包为 Windows EXE（PyInstaller onedir）
rem 产物：dist\PdfDataTool\PdfDataTool.exe（整个 PdfDataTool 目录即绿色软件）
cd /d "%~dp0"
set PYTHONUTF8=1

rem 0) 打包会整体重建 dist\PdfDataTool，先备份用户数据 data\（app.db 等）
set DATA_DIR=dist\PdfDataTool\data
set BAK_DIR=%TEMP%\PdfDataTool_data_backup
if exist "%DATA_DIR%" (
    echo [build] backing up user data -^> %BAK_DIR%
    if exist "%BAK_DIR%" rmdir /S /Q "%BAK_DIR%"
    xcopy "%DATA_DIR%" "%BAK_DIR%" /E /I /Y >nul
)

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
  --icon "assets\app.ico" ^
  --hidden-import pdfplumber ^
  --hidden-import pypdfium2 ^
  --hidden-import sqlalchemy.dialects.sqlite

if errorlevel 1 (
    echo [build] FAILED
    exit /b 1
)

rem 3) 还原用户数据
if exist "%BAK_DIR%" (
    echo [build] restoring user data ...
    xcopy "%BAK_DIR%" "%DATA_DIR%" /E /I /Y >nul
    rmdir /S /Q "%BAK_DIR%"
)

echo.
echo [build] OK -^> dist\PdfDataTool\PdfDataTool.exe
echo [build] 整个 dist\PdfDataTool 目录即可分发；用户数据在 %%APPDATA%%\PdfDataTool，更新不碰数据
