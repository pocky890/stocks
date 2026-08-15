"""研究用一次性腳本：延續backtest_bullish_divergence_user_proposal.py的發現——「右側確認」
(等隔天收盤站上5日均線或前一天最高價)雖然10年數字有改善，但2026-07~08系統性重挫期間
還是0%勝率，追查後發現兩種失敗模式：(1)確認門檻太低，一天雜訊反彈(甚至只贏均線0.1%)
就過關；(2)真的等到像樣的反彈確認，但那只是重挫中的中繼反彈，不是真正的底。

2026-08-15使用者接著建議：右側確認訊號要「多一點」，例如KD、MACD、改用10日均線，這裡
逐項+疊加測試「多一點確認訊號」能不能真的解決模式(1)——不動STRATEGY_REGISTRY的預設
params，只在這裡比較全觀察清單10年/2026 YTD/2026-07~08-15單獨表現。"""
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

CONFIGS = [
    ("原本(無確認)", {}),
    ("現行右側確認(MA5或前高，任一)", {"require_reversal_confirm": True}),
    ("改用MA10(其餘不變)", {"require_reversal_confirm": True, "reversal_confirm_ma_period": 10}),
    ("+KD，任一成立(價格/KD任一)", {"require_reversal_confirm": True, "require_reversal_kd": True}),
    ("+MACD，任一成立(價格/MACD任一)", {"require_reversal_confirm": True, "require_reversal_macd": True}),
    (
        "+KD+MACD，任一成立(3選1)",
        {"require_reversal_confirm": True, "require_reversal_kd": True, "require_reversal_macd": True},
    ),
    (
        "+KD+MACD，至少2個成立(多數決)",
        {
            "require_reversal_confirm": True,
            "require_reversal_kd": True,
            "require_reversal_macd": True,
            "reversal_confirm_min_signals": 2,
        },
    ),
    (
        "+KD+MACD，全部都要(3個AND)",
        {
            "require_reversal_confirm": True,
            "require_reversal_kd": True,
            "require_reversal_macd": True,
            "reversal_confirm_min_signals": 3,
        },
    ),
]
YTD_START = pd.Timestamp("2026-01-01")
RECENT_START = pd.Timestamp("2026-07-01")
RECENT_END = pd.Timestamp("2026-08-15")


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

    rows_10y, rows_ytd, rows_recent = [], [], []
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
            recent_trades.extend([t for t in trades if RECENT_START <= t.entry_ts <= RECENT_END])
        rows_10y.append({"設定": label, **summarize(all_trades)})
        rows_ytd.append({"設定": label, **summarize(ytd_trades)})
        rows_recent.append({"設定": label, **summarize(recent_trades)})

    print("--- 全觀察清單10年 ---")
    print(pd.DataFrame(rows_10y).to_string(index=False))
    print("--- 2026 YTD ---")
    print(pd.DataFrame(rows_ytd).to_string(index=False))
    print("--- 2026-07~08-15 進場 ---")
    print(pd.DataFrame(rows_recent).to_string(index=False))


if __name__ == "__main__":
    main()
