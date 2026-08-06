# 台股訊號監控系統

個人自用工具：對觀察清單即時計算7種技術訊號並推播Telegram，收盤後對全市場批次掃描。不做自動下單。

## 目前狀態

永豐證券帳戶已開通，Shioaji API金鑰也已申請並確認可以登入（模擬模式）。策略邏輯、回測框架、Telegram通知、Streamlit儀表板都已完成並用範例歷史資料驗證過；`shioaji_client.py`的真實連線也已測試成功（`run_batch.py`能正常登入並嘗試抓當日資料）。

`run_live.py`（盤中即時監控）跟`run_batch.py`（收盤後批次掃描）現在都能實際使用——`run_live.py`要在開盤時段(09:00-13:30)手動啟動，`run_batch.py`收盤後(13:30後)跑才會抓到當天資料。

## 環境

已驗證 `shioaji` 在 Python 3.13 上可正常安裝（1.7.1版），不需要額外的相容venv。

## 設定

1. 建立並啟用虛擬環境：`python -m venv .venv`，然後 `.venv\Scripts\activate`（PowerShell用 `.venv\Scripts\Activate.ps1`）
2. `pip install -r requirements.txt`
3. `cp .env.example .env`，填入 `SHIOAJI_API_KEY` / `SHIOAJI_SECRET_KEY` / `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`
4. `python scripts/init_db.py`

## 使用

```bash
python scripts/fetch_historical.py     # 填充範例歷史資料
python scripts/backtest.py             # 跑7種策略統計訊號頻率
streamlit run dashboard/app.py         # 開儀表板
pytest                                  # 跑單元測試
```

即時監控（`run_live.py`，開盤時段09:00-13:30手動啟動）與收盤後全市場批次掃描（`run_batch.py`，13:30後跑）現在都能用真實Shioaji連線。
