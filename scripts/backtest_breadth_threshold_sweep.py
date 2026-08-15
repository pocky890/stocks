"""研究用一次性腳本：backtest_breadth_circuit_breaker.py用60%/40%的寬度斷路器門檻，
觀察清單只有22檔(1檔=4.5個百分點，門檻其實約等於「13~14檔翻空/9檔回穩」這種粗略的
檔數門檻，不是精確比例)——這裡掃一輪不同門檻組合，確認2026年這次的改善不是剛好套中
60%/40%這組數字的巧合，換個門檻結論還站得住腳才算穩健。"""
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

BREADTH_MA_PERIOD = 20
THRESHOLD_PAIRS = [
    (0.50, 0.30),
    (0.55, 0.35),
    (0.60, 0.40),  # 基準(前一版測過的)
    (0.65, 0.45),
    (0.70, 0.50),
]

YTD_START = pd.Timestamp("2026-01-01")
JULY_START = pd.Timestamp("2026-07-01")
JULY_END = pd.Timestamp("2026-07-31")


def compute_breadth_pct(bars_by_symbol: dict) -> pd.Series:
    closes = {code: b["close"] for code, b in bars_by_symbol.items() if not b.empty}
    df = pd.DataFrame(closes).sort_index()
    ma20 = df.rolling(BREADTH_MA_PERIOD).mean()
    return (df < ma20).sum(axis=1) / df.notna().sum(axis=1)


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

    pct_below = compute_breadth_pct(bars_by_symbol)
    strategy_names = sorted(NOTIFIABLE_STRATEGIES)

    # 預先算好「原本(無斷路器)」的事件(依strategy+symbol分開存，不能合併——simulate_round_trips
    # 是依時間序列逐一配對BUY/SELL，把不同股票的事件混在同一個list裡會配對到錯的股票)，
    # 門檻掃描時重複用，不用每個門檻都重算一次策略評估。
    raw_events_by_strategy = {}
    for strategy_name in strategy_names:
        strategy = STRATEGY_REGISTRY[strategy_name]
        events_by_symbol = {}
        for code, name in symbols:
            bars = bars_by_symbol[code]
            if bars.empty:
                continue
            events_by_symbol[code] = strategy.evaluate(code, bars, config.strategy_params.get(strategy_name, {}))
        raw_events_by_strategy[strategy_name] = events_by_symbol

    def run(events_by_strategy, active, start, end):
        all_trades = []
        for strategy_name, events_by_symbol in events_by_strategy.items():
            for code, events in events_by_symbol.items():
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

    baseline_ytd = summarize(run(raw_events_by_strategy, None, YTD_START, None))
    baseline_july = summarize(run(raw_events_by_strategy, None, JULY_START, JULY_END))
    print("=== 原本(無斷路器)基準 ===")
    print("2026 YTD:", baseline_ytd)
    print("2026-07 :", baseline_july)
    print()

    rows_ytd, rows_july, active_days_rows = [], [], []
    for enter, exit_ in THRESHOLD_PAIRS:
        active = active_mask(pct_below, enter, exit_)
        label = f"{enter*100:.0f}%/{exit_*100:.0f}%"

        july_active = active.loc[JULY_START:JULY_END]
        active_days_rows.append({"門檻(進/出)": label, "7月生效天數": int(july_active.sum()), "7月總天數": len(july_active)})

        rows_ytd.append({"門檻(進/出)": label, **summarize(run(raw_events_by_strategy, active, YTD_START, None))})
        rows_july.append({"門檻(進/出)": label, **summarize(run(raw_events_by_strategy, active, JULY_START, JULY_END))})

    print("=== 斷路器生效天數(7月，共23個交易日) ===")
    print(pd.DataFrame(active_days_rows).to_string(index=False))
    print("\n=== 2026 YTD：不同門檻組合 ===")
    print(pd.DataFrame(rows_ytd).to_string(index=False))
    print("\n=== 2026-07單月：不同門檻組合 ===")
    print(pd.DataFrame(rows_july).to_string(index=False))


if __name__ == "__main__":
    main()
