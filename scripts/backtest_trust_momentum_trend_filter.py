"""研究用一次性腳本：trust_momentum(投信買超動能) 2026年YTD在全觀察清單表現很差
(獲利因子0.5、加總報酬-105.4)，追查後發現虧損高度集中在2026-07的一波全市場系統性
重挫——投信照樣買、但個股/大盤趨勢已經轉弱，策略沒有濾網擋，同一時間十幾檔一起
進場又一起停損。這裡比較加上「close > MA60」濾網前後的全觀察清單10年回測表現，
驗證能不能改善這種「籌碼還在買但趨勢已經壞了」的假訊號，不動STRATEGY_REGISTRY的
預設params，只在這裡臨時override，跟backtest_stop_comparison.py同一套模式。"""
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

CONFIGS = [
    ("原本(無趨勢濾網)", {}),
    ("加MA60濾網", {"require_uptrend": True}),
]


def main():
    config = load_config()
    strategy = TrustMomentumStrategy()

    with connect(config.db_path) as conn:
        symbols = [(row["code"], row["name"]) for row in fetch_watchlist(conn)]
        bars_by_symbol = {}
        for code, name in symbols:
            bars = bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date")
            bars_by_symbol[code] = attach_institutional_flows(bars, fetch_institutional_flows(conn, code))

    if not symbols:
        print("watchlist是空的，先跑 scripts/fetch_historical.py 填資料")
        return

    print(f"觀察清單共{len(symbols)}檔\n")

    overall_rows = []
    per_symbol_rows = []

    for label, extra_params in CONFIGS:
        all_trades = []
        for code, name in symbols:
            bars = bars_by_symbol[code]
            if bars.empty or "trust_net" not in bars.columns:
                continue
            events = strategy.evaluate(code, bars, extra_params)
            trades, _ = simulate_round_trips(events)
            all_trades.extend(trades)

            summary = summarize_trades(trades)
            per_symbol_rows.append(
                {
                    "代號": code,
                    "名稱": name,
                    "設定": label,
                    "筆數": summary["n"] if summary else 0,
                    "勝率": round(summary["win_rate"], 1) if summary else None,
                    "加總報酬": round(summary["total_return_pct"], 1) if summary else None,
                    "獲利因子": round(summary["profit_factor"], 2) if summary and summary["profit_factor"] is not None else None,
                }
            )

        summary = summarize_trades(all_trades)
        if summary is None:
            overall_rows.append({"設定": label, "筆數": 0})
            continue
        overall_rows.append(
            {
                "設定": label,
                "筆數": summary["n"],
                "勝率": round(summary["win_rate"], 1),
                "平均報酬": round(summary["avg_return_pct"], 1),
                "加總報酬": round(summary["total_return_pct"], 1),
                "獲利因子": round(summary["profit_factor"], 2) if summary["profit_factor"] is not None else None,
                "最大回撤": round(-summary["max_drawdown_pct"], 1),
            }
        )

    print("=== 整體加總(全觀察清單所有交易併在一起算，10年歷史)：原本 vs 加MA60濾網 ===")
    print(pd.DataFrame(overall_rows).to_string(index=False))

    print("\n=== 逐檔比較(加總報酬/獲利因子，看濾網是普遍改善還是只有少數幾檔在拉) ===")
    per_symbol_df = pd.DataFrame(per_symbol_rows)
    pivot = per_symbol_df.pivot(index=["代號", "名稱"], columns="設定", values=["筆數", "加總報酬", "獲利因子"])
    print(pivot.to_string())


if __name__ == "__main__":
    main()
