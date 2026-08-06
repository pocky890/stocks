@echo off
cd /d "%~dp0"
.venv\Scripts\python.exe scripts\run_batch.py >> logs\run_batch.log 2>&1
