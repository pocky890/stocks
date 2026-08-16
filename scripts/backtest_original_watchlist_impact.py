"""研究用一次性腳本：使用者質疑「你有測原本觀察清單裡的股票嗎？」——今天這一輪所有回測
都是用「全觀察清單10年」(混雜了2026-08-16新加的18支+使用者自己在這次對話前加的
6806/2314/4763，共21支「已知近年很爛」的股票)或「20支已知下跌很兇的股票」兩種範圍，
從來沒有單獨拉出「這次對話開始前，git已經commit的原始28支股票」確認今天新加的這些
濾網(regime/MA240/volume_alert_scaleout)對這些本來體質健康的股票有沒有意外的負面
影響。這裡專門補這個驗證：original_watchlist(28支)vs今天改動前後的config.json params
比較，全觀察清單10年+2026 YTD。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd

from stocks.config import load_config
from stocks.db import attach_institutional_flows, bars_to_dataframe, connect, fetch_bars_daily, fetch_institutional_flows, fetch_watchlist
from stocks.strategies import STRATEGY_REGISTRY
from stocks.strategy_stats import is_scaleout_strategy, simulate_round_trips, simulate_scaleout_trades, summarize_trades

ORIGINAL_WATCHLIST = {
    "1303", "2308", "2313", "2330", "2337", "2383", "2408", "2454", "3037", "3141",
    "3189", "3363", "3443", "3450", "3526", "3595", "3661", "3680", "3711", "6187",
    "6239", "6491", "6531", "6640", "6903", "7769", "8046", "8299",
}
YTD_START = pd.Timestamp("2026-01-01")

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
        symbols = [(row["code"], row["name"]) for row in fetch_watchlist(conn) if row["code"] in ORIGINAL_WATCHLIST]
        bars_by_symbol = {}
        for code, name in symbols:
            bars = bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date")
            bars_by_symbol[code] = attach_institutional_flows(bars, fetch_institutional_flows(conn, code))

    print(f"原始觀察清單({len(symbols)}檔): {sorted(c for c, _ in symbols)}")

    for strategy_name, before_params in BEFORE_PARAMS.items():
        strategy_obj = STRATEGY_REGISTRY[strategy_name]
        after_params = config.strategy_params[strategy_name]
        configs = [("今天改動前", before_params), ("今天改動後(現行)", after_params)]

        print(f"\n{'=' * 20} {strategy_name} {'=' * 20}")
        for scope_label, start in [("原始28支10年", None), ("原始28支2026 YTD", YTD_START)]:
            rows = []
            for label, params in configs:
                scaleout = is_scaleout_strategy(strategy_name, params)
                all_trades = []
                for code, name in symbols:
                    bars = bars_by_symbol[code]
                    if bars.empty:
                        continue
                    events = strategy_obj.evaluate(code, bars, params)
                    if start is not None:
                        events = [e for e in events if e.ts >= start]
                    trades, _ = simulate_scaleout_trades(events) if scaleout else simulate_round_trips(events)
                    all_trades.extend(trades)
                rows.append({"設定": label, **summarize(all_trades)})
            print(f"\n--- {scope_label} ---")
            print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
