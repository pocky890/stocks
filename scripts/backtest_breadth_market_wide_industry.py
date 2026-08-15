"""研究用一次性腳本：使用者決定全部都用「全市場同產業寬度」當斷路器依據(不是用觀察
清單自己的寬度、也不是前一版的混合制)——每支股票依照自己的官方產業代碼(twse/tpex
公司名錄的產業別)，對應到全市場同產業的寬度序列。全市場資料只抓了~9個月
(2025-11-01~2026-08-14，見fetch_market_breadth_data.py)，這裡只驗證2026 YTD/7月，
沒有10年全歷史的穩健性佐證。"""
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
ENTER_THRESHOLD = 0.60
EXIT_THRESHOLD = 0.40

YTD_START = pd.Timestamp("2026-01-01")
JULY_START = pd.Timestamp("2026-07-01")
JULY_END = pd.Timestamp("2026-07-31")


def active_mask(pct_below: pd.Series, enter=ENTER_THRESHOLD, exit_=EXIT_THRESHOLD) -> pd.Series:
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


def breadth_pct(price_df: pd.DataFrame) -> pd.Series:
    ma20 = price_df.rolling(BREADTH_MA_PERIOD).mean()
    return (price_df < ma20).sum(axis=1) / price_df.notna().sum(axis=1)


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

    active_mask_by_industry = {}
    for ind in set(symbol_industry.values()):
        sub = market_df[market_df["industry"] == ind]
        if sub.empty:
            print(f"警告：全市場資料裡沒有產業代碼{ind}，這個類別暫時無法套斷路器")
            continue
        pivot = sub.pivot(index="date", columns="symbol", values="close").sort_index()
        active_mask_by_industry[ind] = active_mask(breadth_pct(pivot))

    # 對照組：前一版「全觀察清單混合」的寬度
    watchlist_close_df = pd.DataFrame({code: b["close"] for code, b in bars_by_symbol.items() if not b.empty})
    whole_watchlist_active = active_mask(breadth_pct(watchlist_close_df))

    strategy_names = sorted(NOTIFIABLE_STRATEGIES)

    def run(mode, start, end):
        all_trades = []
        for strategy_name in strategy_names:
            strategy = STRATEGY_REGISTRY[strategy_name]
            for code, name in symbols:
                bars = bars_by_symbol[code]
                if bars.empty:
                    continue
                events = strategy.evaluate(code, bars, config.strategy_params.get(strategy_name, {}))
                if mode == "whole_portfolio":
                    events = [e for e in events if not (e.direction == Direction.BUY and whole_watchlist_active.get(e.ts, False))]
                elif mode == "market_wide_industry":
                    active = active_mask_by_industry.get(symbol_industry.get(code))
                    if active is not None:
                        events = [e for e in events if not (e.direction == Direction.BUY and active.get(e.ts, False))]
                if start is not None:
                    events = [e for e in events if e.ts >= start]
                if end is not None:
                    events = [e for e in events if e.ts <= end]
                trades, _ = simulate_round_trips(events)
                all_trades.extend(trades)
        return all_trades

    for scope_label, start, end in [("2026 YTD", YTD_START, None), ("2026-07單月", JULY_START, JULY_END)]:
        rows = []
        for mode_label, mode in [
            ("原本(無斷路器)", "none"),
            ("全觀察清單混合寬度", "whole_portfolio"),
            ("全市場同產業寬度", "market_wide_industry"),
        ]:
            rows.append({"設定": mode_label, **summarize(run(mode, start, end))})
        print(f"\n=== {scope_label} ===")
        print(pd.DataFrame(rows).to_string(index=False))

    # 逐檔攤開看(2026 YTD)，確認不是被某一兩檔拉出來的
    print("\n=== 逐檔明細(2026 YTD，加總報酬) ===")
    per_symbol_rows = []
    for code, name in symbols:
        row = {"代號": code, "名稱": name, "產業代碼": symbol_industry.get(code)}
        for mode_label, mode in [
            ("原本", "none"),
            ("全觀察清單混合寬度", "whole_portfolio"),
            ("全市場同產業寬度", "market_wide_industry"),
        ]:
            trades_all = []
            for strategy_name in strategy_names:
                strategy = STRATEGY_REGISTRY[strategy_name]
                bars = bars_by_symbol[code]
                if bars.empty:
                    continue
                events = strategy.evaluate(code, bars, config.strategy_params.get(strategy_name, {}))
                if mode == "whole_portfolio":
                    events = [e for e in events if not (e.direction == Direction.BUY and whole_watchlist_active.get(e.ts, False))]
                elif mode == "market_wide_industry":
                    active = active_mask_by_industry.get(symbol_industry.get(code))
                    if active is not None:
                        events = [e for e in events if not (e.direction == Direction.BUY and active.get(e.ts, False))]
                events = [e for e in events if e.ts >= YTD_START]
                trades, _ = simulate_round_trips(events)
                trades_all.extend(trades)
            s = summarize(trades_all)
            row[f"{mode_label}_筆數"] = s.get("筆數", 0)
            row[f"{mode_label}_加總報酬"] = s.get("加總報酬")
        per_symbol_rows.append(row)
    print(pd.DataFrame(per_symbol_rows).to_string(index=False))


if __name__ == "__main__":
    main()
