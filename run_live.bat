@echo off
cd /d "%~dp0"
.venv\Scripts\python.exe scripts\run_live.py >> logs\run_live.log 2>&1
