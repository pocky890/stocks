@echo off
cd /d "%~dp0"
.venv\Scripts\python.exe scripts\backfill_missing_watchlist_data.py >> logs\backfill_missing_watchlist_data.log 2>&1
