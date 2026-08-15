"""研究用一次性腳本：backtest_breadth_market_wide_industry.py固定用60%/40%門檻，
這裡掃一輪不同門檻組合(純產業寬度版本)，用2年期資料看能不能找到比60/40更好的總報酬/
風險平衡點，或至少確認60/40不是隨便選的、換個門檻結論方向不會亂跳。"""
import json
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
from stocks.models import Direction
from stocks.notifier import NOTIFIABLE_STRATEGIES
from stocks.strategies import STRATEGY_REGISTRY
from stocks.strategy_stats import simulate_round_trips, summarize_trades

SCRATCH = Path(
    r"C:\Users\pocky\AppData\Local\Temp\claude\D--Claude-project-Stocks\a748ed0e-71e0-474d-b026-96dccdce6cc7\scratchpad"
)
BREADTH_MA_PERIOD = 20
THRESHOLD_PAIRS = [(0.50, 0.30), (0.55, 0.35), (0.60, 0.40), (0.65, 0.45), (0.70, 0.50)]

TWO_YEAR_START = pd.Timestamp("2024-08-15")
JULY_START = pd.Timestamp("2026-07-01")
JULY_END = pd.Timestamp("2026-07-31")


def breadth_pct(price_df: pd.DataFrame) -> pd.Series:
    ma20 = price_df.rolling(BREADTH_MA_PERIOD).mean()
    return (price_df < ma20).sum(axis=1) / price_df.notna().sum(axis=1)


def active_mask(pct_below: pd.Series, enter: float, exit_: float) -> pd.Series:
    active = pd.Series(False, index=pct_below.index)
    state = False
    for t, v in pct_below.items():
        if not pd.isna(v):
            if not state and v >= enter:
                state = True
            elif state and v <= exit_:
                state = False
        active[t] = state
    return active


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

    with open(SCRATCH / "industry_map.json", encoding="utf-8") as f:
        industry_map = json.load(f)

    with connect(config.db_path) as conn:
        symbols = [(row["code"], row["name"]) for row in fetch_watchlist(conn)]
        bars_by_symbol = {}
        for code, name in symbols:
            bars = bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date")
            bars_by_symbol[code] = attach_institutional_flows(bars, fetch_institutional_flows(conn, code))

    symbol_industry = {code: industry_map.get(code) for code, name in symbols}

    market_df = pd.read_parquet(SCRATCH / "market_breadth_closes.parquet")
    market_df["date"] = pd.to_datetime(market_df["date"])
    pct_below_by_industry = {}
    for ind in set(symbol_industry.values()):
        sub = market_df[market_df["industry"] == ind]
        if sub.empty:
            continue
        pivot = sub.pivot(index="date", columns="symbol", values="close").sort_index()
        pct_below_by_industry[ind] = breadth_pct(pivot)

    # 每個strategy+symbol的原始事件，先算好重複用
    strategy_names = sorted(NOTIFIABLE_STRATEGIES)
    raw_events = {}
    for strategy_name in strategy_names:
        strategy = STRATEGY_REGISTRY[strategy_name]
        raw_events[strategy_name] = {}
        for code, name in symbols:
            bars = bars_by_symbol[code]
            if bars.empty:
                continue
            raw_events[strategy_name][code] = strategy.evaluate(code, bars, config.strategy_params.get(strategy_name, {}))

    def run(active_mask_by_industry, start, end):
        all_trades = []
        for strategy_name, by_symbol in raw_events.items():
            for code, events in by_symbol.items():
                active = active_mask_by_industry.get(symbol_industry.get(code)) if active_mask_by_industry else None
                filtered = events
                if active is not None:
                    filtered = [e for e in filtered if not (e.direction == Direction.BUY and active.get(e.ts, False))]
                if start is not None:
                    filtered = [e for e in filtered if e.ts >= start]
                if end is not None:
                    filtered = [e for e in filtered if e.ts <= end]
                trades, _ = simulate_round_trips(filtered)
                all_trades.extend(trades)
        return all_trades

    baseline_2y = summarize(run(None, TWO_YEAR_START, None))
    baseline_july = summarize(run(None, JULY_START, JULY_END))
    print("=== 原本(無斷路器)基準 ===")
    print("近2年:", baseline_2y)
    print("2026-07:", baseline_july)

    rows_2y, rows_july = [], []
    for enter, exit_ in THRESHOLD_PAIRS:
        active_mask_by_industry = {ind: active_mask(pct, enter, exit_) for ind, pct in pct_below_by_industry.items()}
        label = f"{enter*100:.0f}%/{exit_*100:.0f}%"
        rows_2y.append({"門檻(進/出)": label, **summarize(run(active_mask_by_industry, TWO_YEAR_START, None))})
        rows_july.append({"門檻(進/出)": label, **summarize(run(active_mask_by_industry, JULY_START, JULY_END))})

    print("\n=== 近2年：不同門檻(全市場同產業寬度) ===")
    print(pd.DataFrame(rows_2y).to_string(index=False))
    print("\n=== 2026-07單月：不同門檻 ===")
    print(pd.DataFrame(rows_july).to_string(index=False))


if __name__ == "__main__":
    main()
