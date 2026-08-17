"""比較golden_cross原本的均線分批出場(ma_scaleout) vs 單一15%移動停損全出
(pct)——兩種事件形狀不同，原本是一買配兩賣(simulate_scaleout_trades)，pct版是
一買配一賣(simulate_round_trips)。研究用一次性腳本。
"""
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
from stocks.strategies.golden_cross import GoldenCrossStrategy
from stocks.strategy_stats import simulate_round_trips, simulate_scaleout_trades, summarize_trades

CONFIGS = [
    ("ma_scaleout(現行)", {}, simulate_scaleout_trades),
    ("pct15%全出", {"stop_mode": "pct", "stop_pct": 0.15}, simulate_round_trips),
]


def main():
    config = load_config()
    strategy = GoldenCrossStrategy()

    with connect(config.db_path) as conn:
        symbols = [(row["code"], row["name"]) for row in fetch_watchlist(conn)]
        bars_by_symbol = {}
        for code, name in symbols:
            bars = bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date")
            bars_by_symbol[code] = attach_institutional_flows(bars, fetch_institutional_flows(conn, code))

    if not symbols:
        print("watchlist是空的，先跑 scripts/fetch_historical.py 填資料")
        return

    print(f"觀察清單共{len(symbols)}檔\n")

    overall_rows = []
    for cfg_label, cfg_params, simulate_fn in CONFIGS:
        all_trades = []
        for code, name in symbols:
            bars = bars_by_symbol[code]
            if bars.empty:
                continue
            events = strategy.evaluate(code, bars, cfg_params)
            trades, _ = simulate_fn(events)
            all_trades.extend(trades)

        summary = summarize_trades(all_trades)
        if summary is None:
            overall_rows.append({"出場": cfg_label, "筆數": 0})
            continue
        overall_rows.append(
            {
                "出場": cfg_label,
                "筆數": summary["n"],
                "勝率": round(summary["win_rate"], 1),
                "平均報酬": round(summary["avg_return_pct"], 1),
                "加總報酬": round(summary["total_return_pct"], 1),
                "獲利因子": round(summary["profit_factor"], 2) if summary["profit_factor"] is not None else None,
                "最大回撤": round(-summary["max_drawdown_pct"], 1),
            }
        )

    print("=== golden_cross：ma_scaleout(現行) vs pct15%全出 ===")
    print(pd.DataFrame(overall_rows).to_string(index=False))


if __name__ == "__main__":
    main()
