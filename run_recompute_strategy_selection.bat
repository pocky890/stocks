@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
.venv\Scripts\python.exe scripts\recompute_strategy_selection.py >> logs\recompute_strategy_selection.log 2>&1
