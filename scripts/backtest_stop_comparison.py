"""比較既有6個「策略」(atr_breakout/chip_momentum/trust_momentum/breakout/
trend_following/long_swing)在原本出場邏輯 vs 固定15%移動停損下的表現——
golden_cross用均線分批出場、跟ATR/固定%停損結構不同，不在這次比較範圍。
研究用一次性腳本，不動STRATEGY_REGISTRY的預設params，只在這裡臨時override。
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
from stocks.strategies.atr_breakout import ATRBreakoutStrategy
from stocks.strategies.breakout import BreakoutStrategy
from stocks.strategies.chip_momentum import ChipMomentumStrategy
from stocks.strategies.long_swing import LongSwingStrategy
from stocks.strategies.trend_following import TrendFollowingStrategy
from stocks.strategies.trust_momentum import TrustMomentumStrategy
from stocks.strategy_stats import simulate_round_trips, summarize_trades

CANDIDATES = [
    ATRBreakoutStrategy(),
    ChipMomentumStrategy(),
    TrustMomentumStrategy(),
    BreakoutStrategy(),
    TrendFollowingStrategy(),
    LongSwingStrategy(),
]

STOP_CONFIGS = [
    ("原本出場邏輯", {}),
    ("pct15%移動停損", {"stop_mode": "pct", "stop_pct": 0.15}),
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
                overall_rows.append({"策略": strategy.name, "出場": stop_label, "筆數": 0})
                continue
            overall_rows.append(
                {
                    "策略": strategy.name,
                    "出場": stop_label,
                    "筆數": summary["n"],
                    "勝率": round(summary["win_rate"], 1),
                    "平均報酬": round(summary["avg_return_pct"], 1),
                    "加總報酬": round(summary["total_return_pct"], 1),
                    "獲利因子": round(summary["profit_factor"], 2) if summary["profit_factor"] is not None else None,
                    "最大回撤": round(-summary["max_drawdown_pct"], 1),
                }
            )

    print("=== 整體加總(全觀察清單所有交易併在一起算)：原本出場 vs 15%移動停損 ===")
    print(pd.DataFrame(overall_rows).to_string(index=False))


if __name__ == "__main__":
    main()
