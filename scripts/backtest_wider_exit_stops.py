"""研究用一次性腳本：使用者提議「進場濾網加這麼多了(regime/MA240/營收年增率)，訊號應該
更精準，出場是不是可以放寬一點，讓報酬變高」——這是合理的假設(進場濾網已經先篩掉大部分
雜訊，停損不用像以前那麼緊，也許能少幾次被正常回檔洗出場)，但沒測過，這裡實測。

固定現行的進場濾網跟參數不動，只調寬各策略的出場停損寬度，全觀察清單10年+2026YTD比較：
  - atr_breakout/golden_cross_scaleout: stop_pct(現行0.15) 拉寬到0.20/0.25
  - breakout: stop_mode="atr"，拉寬atr_multiplier(現行2)到2.5/3
  - chip_momentum/trust_momentum: stop_mode="volume_alert_scaleout"，剩餘半倉的stop_pct
    (現行0.15)拉寬到0.20/0.25；另外也測alert_volume_multiplier(現行1.5，爆量出貨警示的
    觸發門檻)拉高到2.0，讓策略對「初次爆量」更寬容、賣半倉賣得更晚
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
        "atr_breakout": [
            ("現行(15%)", {}),
            ("拉寬到20%", {"stop_pct": 0.20}),
            ("拉寬到25%", {"stop_pct": 0.25}),
        ],
        "golden_cross_scaleout": [
            ("現行(15%)", {}),
            ("拉寬到20%", {"stop_pct": 0.20}),
            ("拉寬到25%", {"stop_pct": 0.25}),
        ],
        "breakout": [
            ("現行(2倍ATR)", {}),
            ("拉寬到2.5倍ATR", {"atr_multiplier": 2.5}),
            ("拉寬到3倍ATR", {"atr_multiplier": 3}),
        ],
        "chip_momentum": [
            ("現行(15%/1.5倍量)", {}),
            ("剩餘半倉拉寬到20%", {"stop_pct": 0.20}),
            ("剩餘半倉拉寬到25%", {"stop_pct": 0.25}),
            ("爆量門檻拉高到2.0倍", {"alert_volume_multiplier": 2.0}),
        ],
        "trust_momentum": [
            ("現行(15%/1.5倍量)", {}),
            ("剩餘半倉拉寬到20%", {"stop_pct": 0.20}),
            ("剩餘半倉拉寬到25%", {"stop_pct": 0.25}),
            ("爆量門檻拉高到2.0倍", {"alert_volume_multiplier": 2.0}),
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
