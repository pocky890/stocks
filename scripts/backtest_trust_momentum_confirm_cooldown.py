"""研究用一次性腳本：測試使用者(透過另一個AI分析)對trust_momentum(投信買超動能)提出的
兩個修正方向——(1)進場加價格確認(收盤價>5MA或20MA，程式碼裡已有require_uptrend可以
直接測)、(2)停損出場後加N天冷卻期(cooldown_days，本次新增的研究參數，防止投信左側
攤平時被連續巴來巴去)——全觀察清單10年+2026 YTD/7月回測比較，不動STRATEGY_REGISTRY
的預設params。"""
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
from stocks.strategies.trust_momentum import TrustMomentumStrategy
from stocks.strategy_stats import simulate_round_trips, summarize_trades

YTD_START = pd.Timestamp("2026-01-01")
JULY_START = pd.Timestamp("2026-07-01")
JULY_END = pd.Timestamp("2026-07-31")


def build_configs(base_params):
    return [
        ("現行config.json", base_params),
        ("+站上5MA確認", {**base_params, "require_uptrend": True, "trend_ma_period": 5}),
        ("+站上20MA確認", {**base_params, "require_uptrend": True, "trend_ma_period": 20}),
        ("+冷卻5天", {**base_params, "cooldown_days": 5}),
        ("+冷卻10天", {**base_params, "cooldown_days": 10}),
        ("+5MA確認+冷卻5天", {**base_params, "require_uptrend": True, "trend_ma_period": 5, "cooldown_days": 5}),
        ("+20MA確認+冷卻5天", {**base_params, "require_uptrend": True, "trend_ma_period": 20, "cooldown_days": 5}),
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
    strategy = TrustMomentumStrategy()
    base_params = config.strategy_params["trust_momentum"]
    configs = build_configs(base_params)

    with connect(config.db_path) as conn:
        symbols = [(row["code"], row["name"]) for row in fetch_watchlist(conn)]
        bars_by_symbol = {}
        for code, name in symbols:
            bars = bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date")
            bars_by_symbol[code] = attach_institutional_flows(bars, fetch_institutional_flows(conn, code))

    per_symbol_rows = []
    for scope_label, start, end in [("全觀察清單10年", None, None), ("2026 YTD", YTD_START, None), ("2026-07單月", JULY_START, JULY_END)]:
        rows = []
        for label, extra in configs:
            all_trades = []
            for code, name in symbols:
                bars = bars_by_symbol[code]
                if bars.empty or "trust_net" not in bars.columns:
                    continue
                events = strategy.evaluate(code, bars, extra)
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
