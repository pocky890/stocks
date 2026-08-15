"""研究用一次性腳本：延續backtest_bottom_picker_confirmation.py的追查——bullish_divergence/
chip_reversal_fast在2026年7月系統性重挫期間表現很差(進場後平均還要再跌一段才真正落底)，
「隔天價格確認」對chip_reversal_fast是可接受的取捨、對bullish_divergence反而更差。這裡改
測試user建議的方向：額外要求MACD柱狀圖回升/KD呈現K>D(偏多)當進場濾網，不動
STRATEGY_REGISTRY的預設params。"""
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
from stocks.strategies.bullish_divergence import BullishDivergenceStrategy
from stocks.strategies.chip_reversal_fast import ChipReversalFastStrategy
from stocks.strategy_stats import simulate_round_trips, summarize_trades

CANDIDATES = [BullishDivergenceStrategy(), ChipReversalFastStrategy()]
CONFIGS = [
    ("原本", {}),
    ("加MACD回升", {"require_macd_turn": True}),
    ("加KD(K>D)", {"require_kd_bullish": True}),
    ("加MACD+KD", {"require_macd_turn": True, "require_kd_bullish": True}),
]
YTD_START = pd.Timestamp("2026-01-01")
JULY_START = pd.Timestamp("2026-07-01")
JULY_END = pd.Timestamp("2026-07-31")


def summarize(trades):
    summary = summarize_trades(trades)
    if summary is None:
        return {"筆數": 0}
    return {
        "筆數": summary["n"],
        "勝率": round(summary["win_rate"], 1),
        "平均報酬": round(summary["avg_return_pct"], 1),
        "加總報酬": round(summary["total_return_pct"], 1),
        "獲利因子": round(summary["profit_factor"], 2) if summary["profit_factor"] is not None else None,
        "最大回撤": round(-summary["max_drawdown_pct"], 1),
    }


def main():
    config = load_config()

    with connect(config.db_path) as conn:
        symbols = [(row["code"], row["name"]) for row in fetch_watchlist(conn)]
        bars_by_symbol = {}
        for code, name in symbols:
            bars = bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date")
            bars_by_symbol[code] = attach_institutional_flows(bars, fetch_institutional_flows(conn, code))

    if not symbols:
        print("watchlist是空的，先跑 scripts/fetch_historical.py 填資料")
        return

    for strategy in CANDIDATES:
        print(f"\n########## {strategy.name} ##########")
        rows_10y, rows_ytd, rows_july = [], [], []
        for label, extra in CONFIGS:
            all_trades, ytd_trades, july_trades = [], [], []
            for code, name in symbols:
                bars = bars_by_symbol[code]
                if bars.empty:
                    continue
                events = strategy.evaluate(code, bars, extra)
                trades, _ = simulate_round_trips(events)
                all_trades.extend(trades)
                ytd_trades.extend([t for t in trades if t.entry_ts >= YTD_START])
                july_trades.extend([t for t in trades if JULY_START <= t.entry_ts <= JULY_END])
            rows_10y.append({"設定": label, **summarize(all_trades)})
            rows_ytd.append({"設定": label, **summarize(ytd_trades)})
            rows_july.append({"設定": label, **summarize(july_trades)})

        print("--- 全觀察清單10年 ---")
        print(pd.DataFrame(rows_10y).to_string(index=False))
        print("--- 2026 YTD ---")
        print(pd.DataFrame(rows_ytd).to_string(index=False))
        print("--- 2026-07 單月進場 ---")
        print(pd.DataFrame(rows_july).to_string(index=False))


if __name__ == "__main__":
    main()
