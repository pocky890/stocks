"""研究用一次性腳本：使用者建議依「進場屬性」分類套用不同ATR倍數/週期——突破型策略
(atr_breakout/breakout)用3倍ATR(20日週期)當停損，趨勢型/法人動能策略(chip_momentum/
trust_momentum/trend_following/long_swing)用2.5倍ATR(20日週期)。

重要：atr_breakout/chip_momentum/trust_momentum目前正式預設其實是stop_mode="pct"
(15%移動停損)，不是ATR模式——當初backtest_stop_comparison.py驗證過pct比ATR好才改的，
所以這裡「現行預設」欄位對這三支來說就是pct 15%，不是舊的ATR倍數；breakout/trend_
following/long_swing目前正式預設本來就是ATR模式(只是週期/倍數不同)。全觀察清單10年
+2026 YTD/7月回測，不動STRATEGY_REGISTRY的預設params。"""
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

YTD_START = pd.Timestamp("2026-01-01")
JULY_START = pd.Timestamp("2026-07-01")
JULY_END = pd.Timestamp("2026-07-31")

# (策略, 新ATR倍數) —— 週期統一20日
PLAN = [
    (ATRBreakoutStrategy(), 3.0, "突破型"),
    (BreakoutStrategy(), 3.0, "突破型"),
    (ChipMomentumStrategy(), 2.5, "趨勢/法人動能"),
    (TrustMomentumStrategy(), 2.5, "趨勢/法人動能"),
    (TrendFollowingStrategy(), 2.5, "趨勢/法人動能"),
    (LongSwingStrategy(), 2.5, "趨勢/法人動能"),
]


def summarize(trades):
    s = summarize_trades(trades)
    if s is None:
        return {"筆數": 0}
    return {
        "筆數": s["n"],
        "勝率": round(s["win_rate"], 1),
        "平均報酬": round(s["avg_return_pct"], 1),
        "加總報酬": round(s["total_return_pct"], 1),
        "獲利因子": round(s["profit_factor"], 2) if s["profit_factor"] is not None else None,
        "最大回撤": round(-s["max_drawdown_pct"], 1),
    }


def main():
    config = load_config()

    with connect(config.db_path) as conn:
        symbols = [(row["code"], row["name"]) for row in fetch_watchlist(conn)]
        bars_by_symbol = {}
        for code, name in symbols:
            bars = bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date")
            bars_by_symbol[code] = attach_institutional_flows(bars, fetch_institutional_flows(conn, code))

    for strategy, new_multiplier, category in PLAN:
        current_params = config.strategy_params.get(strategy.name, {})
        new_params = {**current_params, "stop_mode": "atr", "atr_period": 20, "atr_multiplier": new_multiplier}

        current_stop_desc = f"stop_mode={current_params.get('stop_mode', 'atr(預設)')}"
        print(f"\n########## {strategy.name}({category}) —— 現行:{current_stop_desc} vs 新ATR(20日, {new_multiplier}倍) ##########")

        for scope_label, start, end in [("全觀察清單10年", None, None), ("2026 YTD", YTD_START, None), ("2026-07單月", JULY_START, JULY_END)]:
            rows = []
            for label, params in [("現行正式預設", current_params), (f"新ATR(20日,{new_multiplier}倍)", new_params)]:
                all_trades = []
                for code, name in symbols:
                    bars = bars_by_symbol[code]
                    if bars.empty:
                        continue
                    events = strategy.evaluate(code, bars, params)
                    if start is not None:
                        events = [e for e in events if e.ts >= start]
                    if end is not None:
                        events = [e for e in events if e.ts <= end]
                    trades, _ = simulate_round_trips(events)
                    all_trades.extend(trades)
                rows.append({"設定": label, **summarize(all_trades)})
            print(f"--- {scope_label} ---")
            print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
