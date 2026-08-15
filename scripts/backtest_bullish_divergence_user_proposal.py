"""研究用一次性腳本：使用者2026-08-15針對「背離抄底」YTD一直沒抄到底(模擬交易紀錄頁面
顯示大部分已平倉交易都虧損)提出的三個調整方向，逐項+合併測試效果：
1. 參數微調：lookback_days 20->60(觀察窗拉長到一季)、rsi_ceiling 40->30(要求更接近
   極端恐慌才進場)。
2. 右側確認濾網(require_reversal_confirm新param)：不在創新低當天直接進場，等隔天出現
   「收盤站上5日均線」或「收盤價站上前一天最高價(破底翻/吞噬)」才進場。
3. 結構停損(stop_mode="structural"新param)：停損固定在進場K棒低點再往下2%緩衝，不像
   pct/atr那樣逐日往上移動——一旦跌破代表「這裡是底」的假設本身錯了，該直接出場。

不動STRATEGY_REGISTRY的預設params，只在這裡逐項疊加比較全觀察清單10年/2026 YTD/
2026-07單月表現。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd

from stocks.config import load_config
from stocks.db import (
    attach_institutional_flows,
    bars_to_dataframe,
    connect,
    fetch_bars_daily,
    fetch_institutional_flows,
    fetch_watchlist,
)
from stocks.strategies.bullish_divergence import BullishDivergenceStrategy
from stocks.strategy_stats import simulate_round_trips, summarize_trades

TUNED_PARAMS = {"lookback_days": 60, "rsi_ceiling": 30}

CONFIGS = [
    ("原本(20日/RSI<40/15%移動停損)", {}),
    ("只調參數(60日/RSI<30)", {**TUNED_PARAMS}),
    ("只加右側確認", {"require_reversal_confirm": True}),
    ("只改結構停損", {"stop_mode": "structural"}),
    ("參數+右側確認", {**TUNED_PARAMS, "require_reversal_confirm": True}),
    ("參數+結構停損", {**TUNED_PARAMS, "stop_mode": "structural"}),
    ("右側確認+結構停損", {"require_reversal_confirm": True, "stop_mode": "structural"}),
    ("全部疊加", {**TUNED_PARAMS, "require_reversal_confirm": True, "stop_mode": "structural"}),
]
YTD_START = pd.Timestamp("2026-01-01")
JULY_START = pd.Timestamp("2026-07-01")
JULY_END = pd.Timestamp("2026-08-15")


def summarize(trades):
    summary = summarize_trades(trades)
    if summary is None:
        return {"筆數": 0}
    return {
        "筆數": summary["n"],
        "勝率": round(summary["win_rate"], 1),
        "平均報酬": round(summary["avg_return_pct"], 1),
        "加總報酬": round(summary["total_return_pct"], 1),
        "獲利因子": round(summary["profit_factor"], 2) if summary["profit_factor"] is not None else None,
        "最大回撤": round(-summary["max_drawdown_pct"], 1),
    }


def main():
    config = load_config()
    strategy = BullishDivergenceStrategy()

    with connect(config.db_path) as conn:
        symbols = [(row["code"], row["name"]) for row in fetch_watchlist(conn)]
        bars_by_symbol = {}
        for code, name in symbols:
            bars = bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date")
            bars_by_symbol[code] = attach_institutional_flows(bars, fetch_institutional_flows(conn, code))

    if not symbols:
        print("watchlist是空的，先跑 scripts/fetch_historical.py 填資料")
        return

    rows_10y, rows_ytd, rows_aug = [], [], []
    for label, extra in CONFIGS:
        all_trades, ytd_trades, recent_trades = [], [], []
        for code, name in symbols:
            bars = bars_by_symbol[code]
            if bars.empty:
                continue
            events = strategy.evaluate(code, bars, extra)
            trades, _ = simulate_round_trips(events)
            all_trades.extend(trades)
            ytd_trades.extend([t for t in trades if t.entry_ts >= YTD_START])
            recent_trades.extend([t for t in trades if JULY_START <= t.entry_ts <= JULY_END])
        rows_10y.append({"設定": label, **summarize(all_trades)})
        rows_ytd.append({"設定": label, **summarize(ytd_trades)})
        rows_aug.append({"設定": label, **summarize(recent_trades)})

    print("--- 全觀察清單10年 ---")
    print(pd.DataFrame(rows_10y).to_string(index=False))
    print("--- 2026 YTD ---")
    print(pd.DataFrame(rows_ytd).to_string(index=False))
    print("--- 2026-07~08-15 進場 ---")
    print(pd.DataFrame(rows_aug).to_string(index=False))


if __name__ == "__main__":
    main()
