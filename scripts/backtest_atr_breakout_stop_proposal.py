"""研究用一次性腳本：使用者對atr_breakout(ATR動態通道突破)提出的停損修正建議，全觀察清單
10年+2026 YTD/7月回測比較，不動STRATEGY_REGISTRY的預設params：
1. 固定15%停損 vs 2倍/2.5倍ATR移動停損(stop_mode="atr")——比較「一體適用的固定%」跟
   「依個股波動度量身打造」哪個好。
2. 雙重停損(stop_mode="two_stage")：進場先用較窄的初始停損(1.5倍ATR或進場K棒最低點)，
   等獲利超過10%才切換成15%寬幅移動停損，比固定15%初始停損風險更小。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd

from stocks.config import load_config
from stocks.db import bars_to_dataframe, connect, fetch_bars_daily, fetch_watchlist
from stocks.strategies.atr_breakout import ATRBreakoutStrategy
from stocks.strategy_stats import simulate_round_trips, summarize_trades

BASE_PARAMS = {"donchian_period": 20, "require_weekly_trend": True, "weekly_trend_mode": "slope"}
CONFIGS = [
    ("原本(固定15%)", {**BASE_PARAMS, "stop_mode": "pct", "stop_pct": 0.15}),
    ("ATR移動停損2倍", {**BASE_PARAMS, "stop_mode": "atr", "atr_multiplier": 2}),
    ("ATR移動停損2.5倍", {**BASE_PARAMS, "stop_mode": "atr", "atr_multiplier": 2.5}),
    (
        "雙重停損(1.5倍ATR初始→10%切15%寬幅)",
        {**BASE_PARAMS, "stop_mode": "two_stage", "initial_stop_basis": "atr", "initial_stop_atr_multiplier": 1.5, "profit_switch_pct": 0.10, "stop_pct": 0.15},
    ),
    (
        "雙重停損(K棒低點初始→10%切15%寬幅)",
        {**BASE_PARAMS, "stop_mode": "two_stage", "initial_stop_basis": "bar_low", "profit_switch_pct": 0.10, "stop_pct": 0.15},
    ),
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
    strategy = ATRBreakoutStrategy()

    with connect(config.db_path) as conn:
        symbols = [(row["code"], row["name"]) for row in fetch_watchlist(conn)]
        bars_by_symbol = {code: bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date") for code, _ in symbols}

    per_symbol_rows = []
    for scope_label, start, end in [("全觀察清單10年", None, None), ("2026 YTD", YTD_START, None), ("2026-07單月", JULY_START, JULY_END)]:
        rows = []
        for label, params in CONFIGS:
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
