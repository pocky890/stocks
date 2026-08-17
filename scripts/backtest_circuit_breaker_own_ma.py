"""研究用一次性腳本：驗證斷路器「自己是否也跌破均線」這個AND條件，用比breadth_ma_period
(現行:20，用來算全市場同產業寬度)更長的own_ma_period(例如60日/季線)取代，能不能真正
提高對golden_cross/trend_following/long_swing/atr_breakout/breakout這5支
「進場前提本身要求站上短均線」的策略的擋下率——起因是使用者發現「隊長」群組(15檔同產業
半導體設備/封測/基板股)2026年6-7月經歷產業性重挫時，斷路器對這5支策略實測擋下率是0%
(見scripts/circuit_breaker_impact.py即時分析，非本檔案)，懷疑是own_ma跟這些策略的進場
均線幾乎互斥所致。

這裡在全觀察清單10年(檢查會不會誤傷像3711這種「產業寬度觸發但自己沒真的走弱」的逆勢股，
docstring裡提過的原始案例)+隊長組(檢查目標問題有沒有解決)兩個範圍，比較own_ma_period=
20(現行)/40/60/120這幾組，用「擋下率」+「有無過度擠殺原本賺錢的訊號」雙重標準判斷。
不動config.json的預設值(現行仍是20)。"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd

from stocks.config import load_config
from stocks.db import (
    connect, bars_to_dataframe, fetch_bars_daily, fetch_institutional_flows,
    attach_institutional_flows, fetch_all_industry_codes, fetch_watchlist,
)
from stocks.circuit_breaker import compute_breadth_series, replay_active_state, CIRCUIT_BREAKER_EXEMPT_STRATEGIES
from stocks.strategies import STRATEGY_REGISTRY
from stocks.strategy_stats import is_scaleout_strategy, simulate_round_trips, simulate_scaleout_trades, summarize_trades
from stocks.notifier import NOTIFIABLE_STRATEGIES
from stocks.models import Direction

TARGET_STRATEGIES = ["golden_cross", "trend_following", "long_swing", "atr_breakout", "breakout"]
OWN_MA_CANDIDATES = [20, 40, 60, 120, None]  # None = 純看產業寬度，拿掉own MA的AND條件


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
    }


def main():
    config = load_config()
    with connect(config.db_path) as conn:
        all_symbols = [(row["code"], row["name"]) for row in fetch_watchlist(conn)]
        industry_map = fetch_all_industry_codes(conn)
        cur = conn.execute("SELECT code, groups FROM symbols WHERE is_watchlist=1")
        captain_codes = {code for code, groups in cur.fetchall() if groups and "隊長" in json.loads(groups)}

        bars_by_symbol = {}
        for code, name in all_symbols:
            bars = bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date")
            bars_by_symbol[code] = attach_institutional_flows(bars, fetch_institutional_flows(conn, code))

        breadth_active_cache = {}
        for code in {industry_map.get(c) for c, _ in all_symbols if industry_map.get(c)}:
            breadth = compute_breadth_series(conn, code, config.circuit_breaker_ma_period)
            breadth_active_cache[code] = replay_active_state(
                breadth, config.circuit_breaker_enter_threshold, config.circuit_breaker_exit_threshold
            ).shift(1).fillna(False)

    def buy_suppressed(code, own_ma_period, ts):
        industry_code = industry_map.get(code)
        if industry_code is None or industry_code not in breadth_active_cache:
            return False
        active_state = breadth_active_cache[industry_code]
        if ts not in active_state.index or not active_state.loc[ts]:
            return False
        if own_ma_period is None:  # 純看產業寬度，不要求自己也跌破均線
            return True
        bars = bars_by_symbol[code]
        own_ma = bars["close"].rolling(own_ma_period).mean()
        if ts not in own_ma.index or pd.isna(own_ma.loc[ts]):
            return False
        return bool(bars["close"].loc[ts] < own_ma.loc[ts])

    for scope_label, code_filter in [("全觀察清單10年", lambda c: True), ("隊長組(2026-05起)", lambda c: c in captain_codes)]:
        print(f"\n{'=' * 20} {scope_label} {'=' * 20}")
        start = pd.Timestamp("2026-05-01") if "隊長" in scope_label else None
        for own_ma_period in OWN_MA_CANDIDATES:
            rows = []
            total_buys = 0
            total_suppressed = 0
            for strategy_name in TARGET_STRATEGIES:
                strategy_obj = STRATEGY_REGISTRY[strategy_name]
                params = config.strategy_params[strategy_name]
                scaleout = is_scaleout_strategy(strategy_name, params)
                all_trades = []
                for code, name in all_symbols:
                    if not code_filter(code):
                        continue
                    bars = bars_by_symbol[code]
                    if bars.empty:
                        continue
                    events = strategy_obj.evaluate(code, bars, params)
                    if start is not None:
                        events = [e for e in events if e.ts >= start]
                    buys_before = [e for e in events if e.direction == Direction.BUY]
                    total_buys += len(buys_before)
                    filtered = [
                        e for e in events
                        if e.direction != Direction.BUY or not buy_suppressed(code, own_ma_period, e.ts)
                    ]
                    total_suppressed += len(buys_before) - len([e for e in filtered if e.direction == Direction.BUY])
                    trades, _ = simulate_scaleout_trades(filtered) if scaleout else simulate_round_trips(filtered)
                    all_trades.extend(trades)
                rows.append({"策略": strategy_name, **summarize(all_trades)})
            supp_pct = total_suppressed / total_buys * 100 if total_buys else 0
            print(f"\n--- own_ma_period={own_ma_period} (整體擋下率 {total_suppressed}/{total_buys}={supp_pct:.1f}%) ---")
            print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
