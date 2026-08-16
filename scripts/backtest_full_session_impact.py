"""研究用一次性腳本：使用者看完這一整輪(volume_alert_scaleout+60/120regime+240年線+
月營收年增率，共5支策略chip_momentum/trust_momentum/golden_cross_scaleout/atr_breakout/
breakout疊加起來)後說「我覺得整體報酬掉很多」——之前只驗證過原始28支股票、或20支已知
下跌股這兩個子集合的個別策略數字，從來沒有算過「全觀察清單51檔、5支策略加總」這個
使用者實際會在dashboard感受到的整體數字對比。這裡把BEFORE_PARAMS(這次session開始前
git commit 6743428的params，跟backtest_original_watchlist_impact.py共用同一份定義)
vs 現行config，套用在完整現行觀察清單上，逐策略+全部加總一起看，10年+2026 YTD兩個
範圍，同時列出總報酬/獲利因子/最大回撤三個指標，不是只看總報酬這一個數字。"""
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

# 跟scripts/backtest_original_watchlist_impact.py的BEFORE_PARAMS完全一致——這次session
# 開始前(git commit 6743428)這5支策略的params，作為「這一整輪改動前」的基準。
BEFORE_PARAMS = {
    "atr_breakout": {
        "donchian_period": 20, "stop_mode": "pct", "stop_pct": 0.15, "atr_period": 14,
        "atr_multiplier": 2, "require_weekly_trend": True, "weekly_trend_mode": "slope",
    },
    "chip_momentum": {
        "chip_streak_days": 5, "rsi_period": 14, "rsi_overbought": 70, "stop_mode": "pct",
        "stop_pct": 0.15, "atr_period": 14, "atr_multiplier": 2.5, "entry_mode": "ratio",
        "ratio_window_days": 5, "ratio_threshold": 0.10,
    },
    "trust_momentum": {
        "chip_window_days": 5, "chip_min_buy_days": 3, "rsi_period": 14, "rsi_overbought": 70,
        "stop_mode": "pct", "stop_pct": 0.15, "atr_period": 14, "atr_multiplier": 2.5,
        "entry_mode": "window10_3", "cum_window_days": 15,
    },
    "golden_cross_scaleout": {
        "fast": 5, "mid": 10, "slow": 20, "chip_lookback_days": 5, "high_lookback_days": 20,
        "volume_avg_period": 20, "score_ma_cross": 2, "score_above_slow": 1, "score_chip": 2,
        "score_breakout": 2, "score_volume": 1, "score_rsi": 1, "rsi_period": 14,
        "rsi_overbought": 70, "score_threshold": 5, "stop_mode": "pct", "stop_pct": 0.15,
    },
    "breakout": {
        "high_lookback_days": 20, "low_lookback_days": 10, "volume_avg_period": 20,
        "volume_multiplier": 1.5, "atr_period": 14, "atr_multiplier": 2,
        "require_weekly_trend": True, "weekly_trend_mode": "slope",
    },
}


def summarize(trades):
    s = summarize_trades(trades)
    if s is None:
        return {"筆數": 0, "加總報酬": 0.0, "獲利因子": None, "最大回撤": 0.0}
    return {
        "筆數": s["n"],
        "勝率": round(s["win_rate"], 1),
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
            if bars.empty:
                continue
            bars = attach_institutional_flows(bars, fetch_institutional_flows(conn, code))
            bars = attach_monthly_revenue_growth(bars, [dict(r) for r in fetch_monthly_revenue(conn, code)])
            bars_by_symbol[code] = bars

    print(f"完整現行觀察清單({len(bars_by_symbol)}檔有資料)\n")

    for scope_label, start in [("全觀察清單10年", None), ("全觀察清單2026 YTD", YTD_START)]:
        print(f"\n{'=' * 30} {scope_label} {'=' * 30}")
        grand_before_total = 0.0
        grand_after_total = 0.0
        grand_before_n = 0
        grand_after_n = 0
        rows = []
        for strategy_name, before_params in BEFORE_PARAMS.items():
            strategy_obj = STRATEGY_REGISTRY[strategy_name]
            after_params = config.strategy_params[strategy_name]
            for label, params in [("改動前", before_params), ("現行", after_params)]:
                scaleout = is_scaleout_strategy(strategy_name, params)
                all_trades = []
                for code, bars in bars_by_symbol.items():
                    events = strategy_obj.evaluate(code, bars, params)
                    if start is not None:
                        events = [e for e in events if e.ts >= start]
                    trades, _ = simulate_scaleout_trades(events) if scaleout else simulate_round_trips(events)
                    all_trades.extend(trades)
                summary = summarize(all_trades)
                rows.append({"策略": strategy_name, "設定": label, **summary})
                if label == "改動前":
                    grand_before_total += summary["加總報酬"]
                    grand_before_n += summary["筆數"]
                else:
                    grand_after_total += summary["加總報酬"]
                    grand_after_n += summary["筆數"]

        print(pd.DataFrame(rows).to_string(index=False))
        pct_change = (grand_after_total - grand_before_total) / abs(grand_before_total) * 100 if grand_before_total else float("nan")
        print(
            f"\n5支策略加總: 改動前 {grand_before_n}筆/加總報酬{grand_before_total:+.1f}%"
            f" -> 現行 {grand_after_n}筆/加總報酬{grand_after_total:+.1f}%"
            f"（{pct_change:+.1f}%）"
        )


if __name__ == "__main__":
    main()
