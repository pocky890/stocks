"""研究用一次性腳本：使用者對capitulation_reversal(爆量急殺止穩)提出的停損修正建議，全
觀察清單10年+2026 YTD/7月回測比較，不動STRATEGY_REGISTRY的預設params：
1. 停損從15%移動停損(stop_mode="pct")改成結構停損(stop_mode="structural"：爆量急殺
   當天的最低點再往下5%緩衝，固定不動)。
2. 加上反彈觸及10/20日均線先賣半倉的分批停利(enable_tiered_profit)，剩餘部位停損上移
   保本後改15%寬幅移動停損——一買配兩賣，用simulate_scaleout_trades配對。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd

from stocks.config import load_config
from stocks.db import bars_to_dataframe, connect, fetch_bars_daily, fetch_watchlist
from stocks.strategies.capitulation_reversal import CapitulationReversalStrategy
from stocks.strategy_stats import simulate_round_trips, simulate_scaleout_trades, summarize_trades

BASE_PARAMS = {
    "drop_threshold_pct": -5.0,
    "volume_multiplier": 2.0,
    "avg_volume_period": 20,
    "stop_mode": "pct",
    "stop_pct": 0.15,
    "atr_period": 14,
    "atr_multiplier": 2.5,
}
STRUCTURAL = {"stop_mode": "structural", "structural_stop_buffer_pct": 0.05}
TIERED_20MA = {**STRUCTURAL, "enable_tiered_profit": True, "tiered_ma_period": 20, "move_stop_to_breakeven_after_tier": True}
TIERED_10MA = {**TIERED_20MA, "tiered_ma_period": 10}
CONFIGS = [
    ("原本(15%移動停損)", {**BASE_PARAMS}, False),
    ("只改結構停損(固定不動,無其他出場)", {**BASE_PARAMS, **STRUCTURAL}, False),
    ("結構停損+反彈觸及20MA先賣半倉", {**BASE_PARAMS, **TIERED_20MA}, True),
    ("結構停損+反彈觸及10MA先賣半倉", {**BASE_PARAMS, **TIERED_10MA}, True),
]
YTD_START = pd.Timestamp("2026-01-01")
JULY_START = pd.Timestamp("2026-07-01")
JULY_END = pd.Timestamp("2026-07-31")


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
    strategy = CapitulationReversalStrategy()

    with connect(config.db_path) as conn:
        symbols = [(row["code"], row["name"]) for row in fetch_watchlist(conn)]
        bars_by_symbol = {code: bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date") for code, _ in symbols}

    per_symbol_rows = []
    for scope_label, start, end in [("全觀察清單10年", None, None), ("2026 YTD", YTD_START, None), ("2026-07單月", JULY_START, JULY_END)]:
        rows = []
        for label, params, is_scaleout in CONFIGS:
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
                trades, _ = simulate_scaleout_trades(events) if is_scaleout else simulate_round_trips(events)
                all_trades.extend(trades)
                if scope_label == "全觀察清單10年":
                    per_symbol_rows.append({"代號": code, "名稱": name, "設定": label, **summarize(trades)})
            rows.append({"設定": label, **summarize(all_trades)})
        print(f"\n=== {scope_label} ===")
        print(pd.DataFrame(rows).to_string(index=False))

    print("\n=== 逐檔明細(10年，加總報酬/獲利因子) ===")
    df = pd.DataFrame(per_symbol_rows)
    pivot = df.pivot(index=["代號", "名稱"], columns="設定", values=["筆數", "加總報酬", "獲利因子"])
    print(pivot.to_string())


if __name__ == "__main__":
    main()
