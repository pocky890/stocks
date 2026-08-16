# 專案筆記

台股訊號監控/模擬交易系統。Streamlit dashboard(`dashboard/app.py`) + 策略引擎
(`src/stocks/strategies/`) + 排程腳本(`scripts/run_live.py`盤中、`scripts/run_batch.py`
收盤後)。commit message已經寫得很詳細(這個專案的慣例)，想知道某個決定的來龍去脈，
`git log`本身就是最完整的文件，這裡只記「git本身看不出來、容易漏掉」的事。

## 每台電腦拉新commit後要做的事

**`data/*.db`(SQLite資料庫)是`.gitignore`排除的，每台電腦各自獨立、不會透過git同步。**
這代表：
- 觀察清單股票、K線/籌碼歷史資料、`signal_events`歷史訊號——各機器本來就該有自己的，
  正常跑`run_live.py`/`run_batch.py`/`daily_update`就會累積起來，不用特別處理。
- 但`symbols.disabled_strategies`(每支股票每個策略要不要推播的排除清單)是用
  **當時的策略參數**backtest算出來、寫進這台電腦的DB——如果pull到的commit改了策略的
  預設參數(例如調整RSI門檻、停損%、連續天數這類)，這台電腦的排除清單並不會自動跟著變，
  因為那是`scripts/recompute_strategy_selection.py`手動跑出來的結果，不是git同步的內容。

**所以：只要pull到的commit改了`config.json`裡任何策略的`strategy_params`(或
`strategy_selection.py`的門檻)，都要在這台電腦上重跑一次：**

```bash
python scripts/recompute_strategy_selection.py
```

不跑的話，這台電腦的通知/模擬交易紀錄會繼續用「舊參數當時算出的排除清單」在跑，
即使程式碼裡的策略邏輯已經是新的了。

2026-08-15這批commit(`521d60b`..`2989fd1`)全部都動到了策略參數或排除門檻(背離抄底
RSI/確認機制、外資買超動能連續天數、投信買超動能視窗、投信轉買超搶進整支移除)，pull完
一定要重跑一次上面那行指令。

## 其他這批commit裡值得注意的變化

- `chip_reversal_fast`(投信轉買超搶進)整支被移除——程式碼、STRATEGY_REGISTRY、
  config.json都拿掉了。如果這台電腦的DB裡有股票的`disabled_strategies`還留著這個
  字串殘留，重跑上面那行recompute就會順便清乾淨(不影響任何行為，NOTIFIABLE_STRATEGIES
  已經不含它)。
- 新增了`CIRCUIT_BREAKER_EXEMPT_STRATEGIES`(目前只有`bullish_divergence`)——這支策略
  進場條件本身就是要在股票跌破自己月線時買，跟斷路器的「自己也跌破月線」擋單條件天生
  衝突，已經讓它完全跳過斷路器檢查(`src/stocks/circuit_breaker.py`)。
- `bullish_divergence.py`裡多了幾個目前config.json**沒有**設成true的研究用參數
  (`reversal_confirm_macd_positive`、`reversal_confirm_macd_streak_days`、
  `stop_mode="structural"`)——這些是backtest過但證實沒有比現行設定好、只是保留起來
  方便以後測試用，不是待完成的功能，不用理它們。
