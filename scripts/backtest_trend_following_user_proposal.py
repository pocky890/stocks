"""研究用一次性腳本：使用者對trend_following(趨勢追蹤)提出的三項修正建議，全觀察清單
10年+2026 YTD/7月回測比較，不動STRATEGY_REGISTRY的預設params：
1. 進場量能濾網從>1倍均量提高到>1.5倍/2倍，要求更強的爆量確認。
2. 20日均線出場加緩衝：連續2天收盤跌破，或單日跌幅達3%才立刻確認，過濾單日假跌破雜訊。
3. 停損從「進場價-2倍ATR，固定不動」改成「移動停利：股價自進場後最高點回落1.5倍ATR」。
分別測單一變更(找出哪一項有效)，也測三項合併(使用者原始提案)。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd

from stocks.config import load_config
from stocks.db import bars_to_dataframe, connect, fetch_bars_daily, fetch_watchlist
from stocks.strategies.trend_following import TrendFollowingStrategy
from stocks.strategy_stats import simulate_round_trips, summarize_trades

BASE_PARAMS = {"fast": 20, "slow": 60, "volume_avg_period": 20, "atr_period": 14, "atr_multiplier": 2}
EXIT_BUFFER = {"ma_break_confirm_days": 2, "ma_break_single_day_drop_pct": -3.0}
TRAILING_1_5 = {"stop_mode": "trailing_atr", "trailing_atr_multiplier": 1.5}

CONFIGS = [
    ("原本(基準)", {**BASE_PARAMS}),
    ("只改量能1.5倍", {**BASE_PARAMS, "volume_multiplier": 1.5}),
    ("只改量能2倍", {**BASE_PARAMS, "volume_multiplier": 2.0}),
    ("只改出場緩衝(連2天/單日3%)", {**BASE_PARAMS, **EXIT_BUFFER}),
    ("只改移動停利(1.5倍ATR)", {**BASE_PARAMS, **TRAILING_1_5}),
    ("三項合併(量能1.5倍)", {**BASE_PARAMS, "volume_multiplier": 1.5, **EXIT_BUFFER, **TRAILING_1_5}),
    ("三項合併(量能2倍)", {**BASE_PARAMS, "volume_multiplier": 2.0, **EXIT_BUFFER, **TRAILING_1_5}),
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
    strategy = TrendFollowingStrategy()

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
