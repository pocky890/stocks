"""比較「抓最低點」候選策略(bullish_divergence/capitulation_reversal，原本還有
chip_reversal_fast，2026-08-15使用者要求整支拿掉)在整個觀察清單上的歷史表現，同時比較
每個策略的ATR移動停損 vs 幾組固定百分比移動停損(8%/10%/15%)——這是研究用的一次性腳本，
候選策略還沒決定要不要留、還沒加進STRATEGY_REGISTRY，先直接import類別來跑。
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
from stocks.strategies.bullish_divergence import BullishDivergenceStrategy
from stocks.strategies.capitulation_reversal import CapitulationReversalStrategy
from stocks.strategy_stats import simulate_round_trips, summarize_trades

STOP_CONFIGS = [
    ("atr2.5x(基準)", {}),
    ("pct8%", {"stop_mode": "pct", "stop_pct": 0.08}),
    ("pct10%", {"stop_mode": "pct", "stop_pct": 0.10}),
    ("pct15%", {"stop_mode": "pct", "stop_pct": 0.15}),
]

CANDIDATES = [
    BullishDivergenceStrategy(),
    CapitulationReversalStrategy(),
]


def main():
    config = load_config()

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

    for strategy in CANDIDATES:
        for stop_label, stop_params in STOP_CONFIGS:
            all_trades = []
            for code, name in symbols:
                bars = bars_by_symbol[code]
                if bars.empty:
                    continue
                events = strategy.evaluate(code, bars, stop_params)
                trades, _ = simulate_round_trips(events)
                all_trades.extend(trades)

            summary = summarize_trades(all_trades)
            if summary is None:
                overall_rows.append({"策略": strategy.name, "停損": stop_label, "筆數": 0})
                continue
            overall_rows.append(
                {
                    "策略": strategy.name,
                    "停損": stop_label,
                    "筆數": summary["n"],
                    "勝率": round(summary["win_rate"], 1),
                    "平均報酬": round(summary["avg_return_pct"], 1),
                    "加總報酬": round(summary["total_return_pct"], 1),
                    "獲利因子": round(summary["profit_factor"], 2) if summary["profit_factor"] is not None else None,
                    "最大回撤": round(-summary["max_drawdown_pct"], 1),
                }
            )

    print("=== 整體加總(全觀察清單所有交易併在一起算)：ATR vs 固定百分比停損 ===")
    print(pd.DataFrame(overall_rows).to_string(index=False))


if __name__ == "__main__":
    main()
