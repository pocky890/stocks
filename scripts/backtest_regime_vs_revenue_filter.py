"""研究用一次性腳本：使用者問「營收濾網取代regime濾網，有比較過嗎」——之前的
backtest_revenue_growth_filter.py只測過「regime濾網已經疊加之上，營收濾網再加上去有沒有
增量效益」，從來沒有單獨測過「只用營收濾網、完全不用regime/MA240濾網」能不能達到類似的
保護效果、同時少付一點總報酬的代價。這裡针對chip_momentum/trust_momentum/golden_cross_
scaleout/atr_breakout/breakout這5支，各自產生4個版本(其餘參數都固定用現行config，只切換
這3個布林開關，乾淨地做regime vs 營收的對照)：

  - 都不開(基準)
  - 只開regime家族(require_long_regime，golden_cross_scaleout/atr_breakout/breakout
    另外也含require_above_long_ma，MA240本來就是跟regime同一組「長期位階」濾網疊加使用，
    現行config從來沒有單獨只開MA240不開regime的版本)
  - 只開營收年增率(require_revenue_growth)
  - 兩者都開(現行)

分別在「全觀察清單10年+2026 YTD」(整體代價)跟「20支已知下跌很兇的股票」(這輪濾網原本
要解決的場景：見頂反轉後避免一路撿一路賠)兩個範圍比較，同時列總報酬/獲利因子/最大回撤。"""
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

KNOWN_DECLINERS = {
    "2314", "4763", "8444", "2929", "4426", "8437", "4174", "8044", "1340", "2239",
    "3552", "4529", "8429", "2726", "1338", "1565", "4552", "4416", "8450",
}

STRATEGY_NAMES = ["chip_momentum", "trust_momentum", "golden_cross_scaleout", "atr_breakout", "breakout"]


def make_variant(base_params: dict, use_regime: bool, use_revenue: bool) -> dict:
    p = dict(base_params)
    p["require_long_regime"] = use_regime
    if "require_above_long_ma" in p:
        p["require_above_long_ma"] = use_regime
    p["require_revenue_growth"] = use_revenue
    return p


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
        all_symbols = [(row["code"], row["name"]) for row in fetch_watchlist(conn)]
        bars_by_symbol_all = {}
        for code, name in all_symbols:
            bars = bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date")
            if bars.empty:
                continue
            bars = attach_institutional_flows(bars, fetch_institutional_flows(conn, code))
            bars = attach_monthly_revenue_growth(bars, [dict(r) for r in fetch_monthly_revenue(conn, code)])
            bars_by_symbol_all[code] = bars

    bars_by_symbol_decliners = {code: bars for code, bars in bars_by_symbol_all.items() if code in KNOWN_DECLINERS}
    print(f"全觀察清單: {len(bars_by_symbol_all)}檔　已知下跌股樣本: {len(bars_by_symbol_decliners)}檔\n")

    for strategy_name in STRATEGY_NAMES:
        base_params = config.strategy_params[strategy_name]
        variants = [
            ("都不開(基準)", make_variant(base_params, False, False)),
            ("只開regime家族", make_variant(base_params, True, False)),
            ("只開營收年增率", make_variant(base_params, False, True)),
            ("兩者都開(現行)", make_variant(base_params, True, True)),
        ]
        print(f"\n{'=' * 25} {strategy_name} {'=' * 25}")
        for scope_label, bars_map, start in [
            ("全觀察清單10年", bars_by_symbol_all, None),
            ("全觀察清單2026YTD", bars_by_symbol_all, YTD_START),
            ("20支已知下跌股10年", bars_by_symbol_decliners, None),
        ]:
            rows = run_scope(strategy_name, variants, bars_map, start)
            print(f"\n--- {scope_label} ---")
            print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
