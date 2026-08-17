@echo off
cd /d "%~dp0"
.venv\Scripts\python.exe scripts\check_run_live_heartbeat.py >> logs\check_run_live_heartbeat.log 2>&1
