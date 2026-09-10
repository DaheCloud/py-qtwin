@echo off
chcp 65001 >nul
rem ============================================================
rem 清理旧版 PdfDataTool 写入注册表的界面设置残留
rem 旧版 QSettings 默认后端写入了 HKCU\Software\pdf-project；
rem 新版已改用 INI 文件（%APPDATA%\PdfDataTool\settings.ini），
rem 此脚本仅删除本应用自己的注册表键，不影响系统与其他软件。
rem ============================================================
set "KEY=HKCU\Software\pdf-project"

reg query "%KEY%" >nul 2>nul
if errorlevel 1 (
    echo [clean] 未发现残留：%KEY% 不存在，无需清理。
    pause
    exit /b 0
)

echo [clean] 发现旧版注册表设置，内容如下：
echo ----------------------------------------------------------
reg query "%KEY%" /s
echo ----------------------------------------------------------
echo.
set /p CONFIRM=确认删除以上注册表键？(Y/N)：
if /i not "%CONFIRM%"=="Y" (
    echo [clean] 已取消，未做任何修改。
    pause
    exit /b 0
)

reg delete "%KEY%" /f >nul
if errorlevel 1 (
    echo [clean] 删除失败，请检查权限后重试。
    pause
    exit /b 1
)

echo [clean] 已删除 %KEY%
echo [clean] 完成。新版设置存储于 %%APPDATA%%\PdfDataTool\settings.ini
pause
