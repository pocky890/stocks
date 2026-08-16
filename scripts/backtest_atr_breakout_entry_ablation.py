"""使用者質疑「ATR動態通道突破策略十年的筆數少的不合理，肯定是濾網太緊」——atr_breakout
現行10年只剩204筆(51檔觀察清單，平均每檔10年僅約4筆)。逐項拆解：進場濾網(require_
weekly_trend/require_long_regime/require_above_long_ma/require_revenue_growth)
疊加式ablation(固定現行stop_pct=0.25，只切換進場濾網)，量化每加一道濾網各自砍掉多少筆，
同時對照stop_pct本身(0.15→0.25)拉寬對筆數的獨立影響——區分「筆數變少是進場太嚴」還是
「筆數變少是因為停損拉寬後單筆抱更久、同一段時間內能進場的次數自然變少」這兩種不同機制，
不能只看最終筆數就下結論。全觀察清單10年。"""
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
from stocks.strategy_stats import simulate_round_trips, summarize_trades


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


def run(bars_by_symbol, params):
    strategy_obj = STRATEGY_REGISTRY["atr_breakout"]
    all_trades = []
    for code, bars in bars_by_symbol.items():
        events = strategy_obj.evaluate(code, bars, params)
        trades, _ = simulate_round_trips(events)
        all_trades.extend(trades)
    return summarize(all_trades)


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

    base = dict(config.strategy_params["atr_breakout"])  # 現行(stop_pct=0.25)

    def variant(**overrides):
        p = dict(base)
        p.update(overrides)
        return p

    print("=" * 20 + " 進場濾網疊加式ablation(固定stop_pct=0.25) " + "=" * 20)
    steps = [
        ("完全不設進場濾網(只留唐奇安突破)", variant(
            require_weekly_trend=False, require_long_regime=False,
            require_above_long_ma=False, require_revenue_growth=False,
        )),
        ("+週線趨勢確認", variant(
            require_weekly_trend=True, require_long_regime=False,
            require_above_long_ma=False, require_revenue_growth=False,
        )),
        ("+60/120regime", variant(
            require_weekly_trend=True, require_long_regime=True,
            require_above_long_ma=False, require_revenue_growth=False,
        )),
        ("+240年線", variant(
            require_weekly_trend=True, require_long_regime=True,
            require_above_long_ma=True, require_revenue_growth=False,
        )),
        ("+月營收年增率(現行全部四項)", variant(
            require_weekly_trend=True, require_long_regime=True,
            require_above_long_ma=True, require_revenue_growth=True,
        )),
    ]
    rows = [{"設定": label, **run(bars_by_symbol, params)} for label, params in steps]
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n" + "=" * 20 + " 單獨看每道濾網各自的影響(相對於完全不設) " + "=" * 20)
    single_steps = [
        ("完全不設", variant(require_weekly_trend=False, require_long_regime=False, require_above_long_ma=False, require_revenue_growth=False)),
        ("只開週線趨勢確認", variant(require_weekly_trend=True, require_long_regime=False, require_above_long_ma=False, require_revenue_growth=False)),
        ("只開60/120regime", variant(require_weekly_trend=False, require_long_regime=True, require_above_long_ma=False, require_revenue_growth=False)),
        ("只開240年線", variant(require_weekly_trend=False, require_long_regime=False, require_above_long_ma=True, require_revenue_growth=False)),
        ("只開月營收年增率", variant(require_weekly_trend=False, require_long_regime=False, require_above_long_ma=False, require_revenue_growth=True)),
    ]
    rows2 = [{"設定": label, **run(bars_by_symbol, params)} for label, params in single_steps]
    print(pd.DataFrame(rows2).to_string(index=False))

    print("\n" + "=" * 20 + " 停損寬度本身對筆數的獨立影響(現行全部四項濾網固定) " + "=" * 20)
    stop_steps = [
        ("stop_pct=0.15(原本)", variant(stop_pct=0.15)),
        ("stop_pct=0.20", variant(stop_pct=0.20)),
        ("stop_pct=0.25(現行)", variant(stop_pct=0.25)),
    ]
    rows3 = [{"設定": label, **run(bars_by_symbol, params)} for label, params in stop_steps]
    print(pd.DataFrame(rows3).to_string(index=False))


if __name__ == "__main__":
    main()
