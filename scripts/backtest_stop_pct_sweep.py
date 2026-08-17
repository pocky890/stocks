"""固定百分比移動停損%數的敏感度分析：15%/20%/25%/30%，套在目前用stop_mode="pct"
的策略上(bullish_divergence/capitulation_reversal/atr_breakout/chip_momentum/
trust_momentum/golden_cross)，回答「是不是停損放越寬越好」。
研究用一次性腳本。
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
from stocks.strategies.bullish_divergence import BullishDivergenceStrategy
from stocks.strategies.capitulation_reversal import CapitulationReversalStrategy
from stocks.strategies.chip_momentum import ChipMomentumStrategy
from stocks.strategies.golden_cross import GoldenCrossStrategy
from stocks.strategies.trust_momentum import TrustMomentumStrategy
from stocks.strategy_stats import simulate_round_trips, summarize_trades

CANDIDATES = [
    BullishDivergenceStrategy(),
    CapitulationReversalStrategy(),
    ATRBreakoutStrategy(),
    ChipMomentumStrategy(),
    TrustMomentumStrategy(),
    GoldenCrossStrategy(),
]

STOP_PCTS = [0.15, 0.20, 0.25, 0.30]


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
        for stop_pct in STOP_PCTS:
            all_trades = []
            for code, name in symbols:
                bars = bars_by_symbol[code]
                if bars.empty:
                    continue
                events = strategy.evaluate(code, bars, {"stop_mode": "pct", "stop_pct": stop_pct})
                trades, _ = simulate_round_trips(events)
                all_trades.extend(trades)

            summary = summarize_trades(all_trades)
            if summary is None:
                overall_rows.append({"策略": strategy.name, "停損%": f"{stop_pct * 100:.0f}%", "筆數": 0})
                continue
            overall_rows.append(
                {
                    "策略": strategy.name,
                    "停損%": f"{stop_pct * 100:.0f}%",
                    "筆數": summary["n"],
                    "勝率": round(summary["win_rate"], 1),
                    "平均報酬": round(summary["avg_return_pct"], 1),
                    "加總報酬": round(summary["total_return_pct"], 1),
                    "獲利因子": round(summary["profit_factor"], 2) if summary["profit_factor"] is not None else None,
                    "最大回撤": round(-summary["max_drawdown_pct"], 1),
                }
            )

    print("=== 整體加總(全觀察清單所有交易併在一起算)：15%/20%/25%/30%固定停損比較 ===")
    print(pd.DataFrame(overall_rows).to_string(index=False))


if __name__ == "__main__":
    main()
