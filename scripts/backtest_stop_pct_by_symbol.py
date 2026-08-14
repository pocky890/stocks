"""15%/20%/25%/30%固定停損的比較，這次拆到「每支股票」層級(把7支策略的交易全部併在
一起算，同一支股票不分策略)，檢查是不是每支股票都在25%附近最好，還是有股票特別不一樣。
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
from stocks.strategies.chip_reversal_fast import ChipReversalFastStrategy
from stocks.strategies.golden_cross_scaleout import GoldenCrossScaleOutStrategy
from stocks.strategies.trust_momentum import TrustMomentumStrategy
from stocks.strategy_stats import simulate_round_trips, summarize_trades

CANDIDATES = [
    BullishDivergenceStrategy(),
    CapitulationReversalStrategy(),
    ChipReversalFastStrategy(),
    ATRBreakoutStrategy(),
    ChipMomentumStrategy(),
    TrustMomentumStrategy(),
    GoldenCrossScaleOutStrategy(),
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

    rows = []
    for code, name in symbols:
        bars = bars_by_symbol[code]
        if bars.empty:
            continue
        for stop_pct in STOP_PCTS:
            all_trades = []
            for strategy in CANDIDATES:
                events = strategy.evaluate(code, bars, {"stop_mode": "pct", "stop_pct": stop_pct})
                trades, _ = simulate_round_trips(events)
                all_trades.extend(trades)
            summary = summarize_trades(all_trades)
            if summary is None:
                rows.append({"代號": code, "名稱": name, "停損%": f"{stop_pct * 100:.0f}%", "筆數": 0})
                continue
            rows.append(
                {
                    "代號": code,
                    "名稱": name,
                    "停損%": f"{stop_pct * 100:.0f}%",
                    "筆數": summary["n"],
                    "勝率": round(summary["win_rate"], 1),
                    "平均報酬": round(summary["avg_return_pct"], 1),
                    "加總報酬": round(summary["total_return_pct"], 1),
                    "獲利因子": round(summary["profit_factor"], 2) if summary["profit_factor"] is not None else None,
                }
            )

    df = pd.DataFrame(rows)
    print("=== 每支股票在15/20/25/30%下的表現(7支策略交易併在一起算) ===")
    print(df.to_string(index=False))

    print("\n=== 每支股票加總報酬最高的是哪個% ===")
    best = df.loc[df.groupby("代號")["加總報酬"].idxmax()][["代號", "名稱", "停損%", "加總報酬"]]
    print(best.to_string(index=False))

    print("\n=== 最佳%的分布 ===")
    print(best["停損%"].value_counts().to_string())


if __name__ == "__main__":
    main()
