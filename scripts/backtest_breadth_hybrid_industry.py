"""研究用一次性腳本：延續backtest_breadth_circuit_breaker.py的「全觀察清單混合寬度」
斷路器——使用者指出這樣會誤傷跟主要族群(半導體供應鏈)不相關的少數持股(例如只有1檔的
生技醫療)。這裡改成「混合制」：
- 觀察清單裡樣本夠多的產業(半導體業24/電子零組件業28，各自>=5檔)，直接用觀察清單自己
  的寬度(反應比全市場快，驗證過7月領先全市場2~5個交易日)。
- 樣本太少的產業(通信網路業27只有2檔、生技醫療業22跟塑膠工業03各只有1檔)，改用全市場
  同產業的寬度(避免用1、2檔算出來的寬度退化成單一個股訊號、也避免被其他不相關產業誤傷)。

全市場資料只抓了~9個月(2025-11-01~2026-08-14，見fetch_market_breadth_data.py)，不是
10年，所以這裡只驗證2026 YTD/7月，沒有10年全歷史的穩健性佐證(那部分之前的
backtest_breadth_circuit_breaker.py已經驗證過純觀察清單版本)。"""
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
MIN_WATCHLIST_SAMPLE = 5  # 觀察清單裡同產業>=這個數字，才信任「用自己清單算的寬度」

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
    from collections import Counter

    industry_counts = Counter(symbol_industry.values())
    print("觀察清單各產業檔數：", dict(industry_counts))

    use_market_wide = {ind for ind, n in industry_counts.items() if n < MIN_WATCHLIST_SAMPLE}
    print("樣本不足、改用全市場寬度的產業代碼：", use_market_wide)

    # 觀察清單自己的寬度(全部產業都先算好，樣本夠的產業會用到)
    watchlist_close_df = pd.DataFrame({code: b["close"] for code, b in bars_by_symbol.items() if not b.empty})
    watchlist_pct_below_by_industry = {}
    for ind in industry_counts:
        cols = [code for code in watchlist_close_df.columns if symbol_industry.get(code) == ind]
        if cols:
            watchlist_pct_below_by_industry[ind] = breadth_pct(watchlist_close_df[cols])

    # 全市場寬度(只有樣本不足的產業才需要)
    market_df = pd.read_parquet(SCRATCH / "market_breadth_closes.parquet")
    market_df["date"] = pd.to_datetime(market_df["date"])
    market_pct_below_by_industry = {}
    for ind in use_market_wide:
        sub = market_df[market_df["industry"] == ind]
        if sub.empty:
            continue
        pivot = sub.pivot(index="date", columns="symbol", values="close").sort_index()
        market_pct_below_by_industry[ind] = breadth_pct(pivot)

    # 每個產業決定要用哪個寬度序列，算出對應的active mask
    active_mask_by_industry = {}
    for ind in industry_counts:
        if ind in use_market_wide and ind in market_pct_below_by_industry:
            series = market_pct_below_by_industry[ind]
        else:
            series = watchlist_pct_below_by_industry[ind]
        active_mask_by_industry[ind] = active_mask(series)

    # 每支股票對應到自己產業的active mask(混合制)；另外也算一份「全觀察清單混合」的
    # active mask(前一版方法)當對照組
    whole_watchlist_pct = breadth_pct(watchlist_close_df)
    whole_watchlist_active = active_mask(whole_watchlist_pct)

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
                    active = whole_watchlist_active
                    events = [e for e in events if not (e.direction == Direction.BUY and active.get(e.ts, False))]
                elif mode == "hybrid_industry":
                    ind = symbol_industry.get(code)
                    active = active_mask_by_industry.get(ind)
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
        for mode_label, mode in [("原本(無斷路器)", "none"), ("全觀察清單混合寬度", "whole_portfolio"), ("混合制(產業分開)", "hybrid_industry")]:
            rows.append({"設定": mode_label, **summarize(run(mode, start, end))})
        print(f"\n=== {scope_label} ===")
        print(pd.DataFrame(rows).to_string(index=False))

    # 額外看一下6491(晶碩，生技醫療業，樣本不足類別)自己的訊號有沒有被誤擋
    print("\n=== 6491(晶碩，生技醫療業)自己的訊號，三種模式下的筆數/加總報酬(2026 YTD) ===")
    for mode_label, mode in [("原本(無斷路器)", "none"), ("全觀察清單混合寬度", "whole_portfolio"), ("混合制(產業分開)", "hybrid_industry")]:
        trades_6491 = []
        for strategy_name in strategy_names:
            strategy = STRATEGY_REGISTRY[strategy_name]
            bars = bars_by_symbol.get("6491")
            if bars is None or bars.empty:
                continue
            events = strategy.evaluate("6491", bars, config.strategy_params.get(strategy_name, {}))
            if mode == "whole_portfolio":
                events = [e for e in events if not (e.direction == Direction.BUY and whole_watchlist_active.get(e.ts, False))]
            elif mode == "hybrid_industry":
                active = active_mask_by_industry.get(symbol_industry.get("6491"))
                if active is not None:
                    events = [e for e in events if not (e.direction == Direction.BUY and active.get(e.ts, False))]
            events = [e for e in events if e.ts >= YTD_START]
            trades, _ = simulate_round_trips(events)
            trades_6491.extend(trades)
        print(mode_label, summarize(trades_6491))


if __name__ == "__main__":
    main()
