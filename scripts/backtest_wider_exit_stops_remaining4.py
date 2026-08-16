"""backtest_wider_exit_stops.py只測了NOTIFIABLE_STRATEGIES裡今天有新增regime/MA240/
營收濾網的5支(chip_momentum/trust_momentum/golden_cross_scaleout/atr_breakout/
breakout)，遺漏了另外4支(trend_following/long_swing/bullish_divergence/
capitulation_reversal)——使用者質疑「不是有九個策略，怎麼沒有全測」，這裡補上剩下4支，
不預設「進場沒加嚴就不用測」，直接用同一套「放寬出場」邏輯實測看看：

  - trend_following: stop_mode="atr"(現行)，atr_multiplier(現行2)拉寬到2.5/3
  - long_swing: stop_mode="atr"(現行)，atr_multiplier(現行3.5)拉寬到4/4.5
  - bullish_divergence: stop_mode="structural"+enable_tiered_profit，剩餘半倉的
    stop_pct(現行0.15)拉寬到0.20/0.25
  - capitulation_reversal: 同上，stop_pct(現行0.15)拉寬到0.20/0.25
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd

from stocks.config import load_config
from stocks.db import (
    attach_institutional_flows,
    attach_monthly_revenue_growth,
    bars_to_dataframe,
    connect,
    fetch_bars_daily,
    fetch_institutional_flows,
    fetch_monthly_revenue,
    fetch_watchlist,
)
from stocks.strategies import STRATEGY_REGISTRY
from stocks.strategy_stats import is_scaleout_strategy, simulate_round_trips, simulate_scaleout_trades, summarize_trades

YTD_START = pd.Timestamp("2026-01-01")


def summarize(trades):
    s = summarize_trades(trades)
    if s is None:
        return {"筆數": 0, "勝率": None, "加總報酬": 0.0, "獲利因子": None, "最大回撤": 0.0}
    return {
        "筆數": s["n"],
        "勝率": round(s["win_rate"], 1),
        "加總報酬": round(s["total_return_pct"], 1),
        "獲利因子": round(s["profit_factor"], 2) if s["profit_factor"] is not None else None,
        "最大回撤": round(-s["max_drawdown_pct"], 1),
    }


def run_scope(strategy_name, variants, bars_by_symbol, start):
    strategy_obj = STRATEGY_REGISTRY[strategy_name]
    rows = []
    for label, params in variants:
        scaleout = is_scaleout_strategy(strategy_name, params)
        all_trades = []
        for code, bars in bars_by_symbol.items():
            events = strategy_obj.evaluate(code, bars, params)
            if start is not None:
                events = [e for e in events if e.ts >= start]
            trades, _ = simulate_scaleout_trades(events) if scaleout else simulate_round_trips(events)
            all_trades.extend(trades)
        rows.append({"設定": label, **summarize(all_trades)})
    return rows


def main():
    config = load_config()
    with connect(config.db_path) as conn:
        symbols = [(row["code"], row["name"]) for row in fetch_watchlist(conn)]
        bars_by_symbol = {}
        for code, name in symbols:
            bars = bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date")
            if bars.empty:
                continue
            bars = attach_institutional_flows(bars, fetch_institutional_flows(conn, code))
            bars = attach_monthly_revenue_growth(bars, [dict(r) for r in fetch_monthly_revenue(conn, code)])
            bars_by_symbol[code] = bars
    print(f"全觀察清單: {len(bars_by_symbol)}檔\n")

    variant_specs = {
        "trend_following": [
            ("現行(2倍ATR)", {}),
            ("拉寬到2.5倍ATR", {"atr_multiplier": 2.5}),
            ("拉寬到3倍ATR", {"atr_multiplier": 3}),
        ],
        "long_swing": [
            ("現行(3.5倍ATR)", {}),
            ("拉寬到4倍ATR", {"atr_multiplier": 4}),
            ("拉寬到4.5倍ATR", {"atr_multiplier": 4.5}),
        ],
        "bullish_divergence": [
            ("現行(剩餘半倉15%)", {}),
            ("拉寬到20%", {"stop_pct": 0.20}),
            ("拉寬到25%", {"stop_pct": 0.25}),
        ],
        "capitulation_reversal": [
            ("現行(剩餘半倉15%)", {}),
            ("拉寬到20%", {"stop_pct": 0.20}),
            ("拉寬到25%", {"stop_pct": 0.25}),
        ],
    }

    for strategy_name, overrides_list in variant_specs.items():
        base_params = config.strategy_params[strategy_name]
        variants = [(label, {**base_params, **override}) for label, override in overrides_list]
        print(f"\n{'=' * 25} {strategy_name} {'=' * 25}")
        for scope_label, start in [("全觀察清單10年", None), ("全觀察清單2026YTD", YTD_START)]:
            rows = run_scope(strategy_name, variants, bars_by_symbol, start)
            print(f"\n--- {scope_label} ---")
            print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
