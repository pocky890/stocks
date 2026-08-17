"""比較「單階15%移動停損」vs「分批停損(8%賣一半/15%賣剩餘一半)」在幾支目前用固定
百分比停損的策略上的表現(bullish_divergence/capitulation_reversal/atr_breakout/
chip_momentum/trust_momentum)。分批出場的訊號要用simulate_scaleout_trades
配對(一次BUY配兩次SELL)，不是simulate_round_trips，跟golden_cross用同一套。
研究用一次性腳本，不動STRATEGY_REGISTRY的預設params。
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
from stocks.strategies.trust_momentum import TrustMomentumStrategy
from stocks.strategy_stats import simulate_round_trips, simulate_scaleout_trades, summarize_trades

CANDIDATES = [
    BullishDivergenceStrategy(),
    CapitulationReversalStrategy(),
    ATRBreakoutStrategy(),
    ChipMomentumStrategy(),
    TrustMomentumStrategy(),
]

CONFIGS = [
    ("單階pct15%", {}, simulate_round_trips),
    ("分批8%/15%", {"stop_mode": "tiered_pct", "stop_pct_half": 0.08, "stop_pct_full": 0.15}, simulate_scaleout_trades),
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
                overall_rows.append({"策略": strategy.name, "出場": cfg_label, "筆數": 0})
                continue
            overall_rows.append(
                {
                    "策略": strategy.name,
                    "出場": cfg_label,
                    "筆數": summary["n"],
                    "勝率": round(summary["win_rate"], 1),
                    "平均報酬": round(summary["avg_return_pct"], 1),
                    "加總報酬": round(summary["total_return_pct"], 1),
                    "獲利因子": round(summary["profit_factor"], 2) if summary["profit_factor"] is not None else None,
                    "最大回撤": round(-summary["max_drawdown_pct"], 1),
                }
            )

    print("=== 整體加總(全觀察清單所有交易併在一起算)：單階15% vs 分批8%/15% ===")
    print(pd.DataFrame(overall_rows).to_string(index=False))


if __name__ == "__main__":
    main()
