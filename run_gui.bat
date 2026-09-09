@echo off
rem 固定结构 PDF 识别工具 - GUI 启动器
rem 双击运行；也可从命令行附带 PDF 路径：run_gui.bat samples\contract_sample.pdf
cd /d "%~dp0"
set PYTHONUTF8=1
".venv\Scripts\python.exe" main.py %*
