"""研究用一次性腳本：驗證「月營收年增率轉負時擋掉BUY訊號」這個基本面濾網的效果——
用attach_monthly_revenue_growth()把已經避開look-ahead(FinMind公告日+10天緩衝)的
revenue_yoy_growth接到bars上，對既有策略訊號做「事後過濾」(不修改策略檔案本身，
只是drop掉不符合條件的BUY事件，跟build_paper_trades_for_symbol過濾斷路器BUY同一種
做法)，全觀察清單10年+「已知近年下跌很兇的20支股票」(2314/4763+2026-08-16新加的18支)
兩個範圍比較。測試對象是6支跟長期趨勢/動能相關的策略(chip_momentum/trust_momentum/
golden_cross_scaleout/atr_breakout/breakout/long_swing)——這6支已經(或本來就)有
60/120日長期regime濾網，這裡驗證基本面年增率能不能在regime濾網之上再提供增量的保護。
不動STRATEGY_REGISTRY的預設params，也不修改任何策略檔案。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd

from stocks.config import load_config
from stocks.db import (
    attach_institutional_flows,
    attach_monthly_revenue_growth,
    bars_to_dataframe,
    connect,
    fetch_bars_daily,
    fetch_institutional_flows,
    fetch_monthly_revenue,
    fetch_watchlist,
)
from stocks.models import Direction
from stocks.strategies import STRATEGY_REGISTRY
from stocks.strategy_stats import is_scaleout_strategy, simulate_round_trips, simulate_scaleout_trades, summarize_trades

STRATEGY_NAMES = [
    "chip_momentum",
    "trust_momentum",
    "golden_cross_scaleout",
    "atr_breakout",
    "breakout",
    "long_swing",
]
KNOWN_DECLINERS = {
    "2314", "4763", "8444", "2929", "4426", "8437", "4174", "8044", "1340", "2239",
    "3552", "4529", "8429", "2726", "1338", "1565", "4552", "4416", "8450",
}  # 00664R(反向ETF)故意不列入——它沒有月營收/財報資料(不是公司)，基本面濾網對它必然
# 是no-op(revenue_yoy_growth全程NaN)，列進來會稀釋這個樣本組的訊號，見CLAUDE.md說明。

FILTERS = [
    ("現行(無基本面濾網)", None),
    ("擋掉最近月營收年增率<0%", {"mode": "latest", "threshold": 0.0}),
    ("擋掉最近月營收年增率<-10%", {"mode": "latest", "threshold": -10.0}),
    ("擋掉近3月營收年增率均值<0%", {"mode": "avg3", "threshold": 0.0}),
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


def revenue_ok_series(bars: pd.DataFrame, filter_spec: dict | None) -> pd.Series:
    """回傳布林Series：True代表這天可以BUY(基本面沒有擋)。NaN(還不知道年增率，例如
    資料還沒回補到、或這支股票根本沒有財報如00664R)一律當作「未知不擋」，不主動排除，
    跟circuit_breaker.py對缺資料industry的處理原則一致(缺資料時保守地不生效，而不是
    保守地全擋)。"""
    if filter_spec is None:
        return pd.Series(True, index=bars.index)
    growth = bars["revenue_yoy_growth"]
    if filter_spec["mode"] == "avg3":
        growth = growth.rolling(3).mean()
    ok = growth >= filter_spec["threshold"]
    return ok | growth.isna()


def filter_buy_events(events, ok_series: pd.Series):
    return [e for e in events if e.direction != Direction.BUY or ok_series.get(e.ts, True)]


def main():
    config = load_config()
    with connect(config.db_path) as conn:
        symbols = [(row["code"], row["name"]) for row in fetch_watchlist(conn)]
        bars_by_symbol = {}
        for code, name in symbols:
            bars = bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date")
            bars = attach_institutional_flows(bars, fetch_institutional_flows(conn, code))
            bars = attach_monthly_revenue_growth(bars, [dict(r) for r in fetch_monthly_revenue(conn, code)])
            bars_by_symbol[code] = bars

    for strategy_name in STRATEGY_NAMES:
        strategy_obj = STRATEGY_REGISTRY[strategy_name]
        params = config.strategy_params[strategy_name]
        scaleout = is_scaleout_strategy(strategy_name, params)

        print(f"\n{'=' * 20} {strategy_name} {'=' * 20}")

        for scope_label, code_filter in [("全觀察清單10年", lambda c: True), ("20支已知近年下跌很兇的股票", lambda c: c in KNOWN_DECLINERS)]:
            rows = []
            for label, filter_spec in FILTERS:
                all_trades = []
                for code, name in symbols:
                    if not code_filter(code):
                        continue
                    bars = bars_by_symbol[code]
                    if bars.empty:
                        continue
                    events = strategy_obj.evaluate(code, bars, params)
                    ok = revenue_ok_series(bars, filter_spec)
                    events = filter_buy_events(events, ok)
                    trades, _ = simulate_scaleout_trades(events) if scaleout else simulate_round_trips(events)
                    all_trades.extend(trades)
                rows.append({"設定": label, **summarize(all_trades)})
            print(f"\n--- {scope_label} ---")
            print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
