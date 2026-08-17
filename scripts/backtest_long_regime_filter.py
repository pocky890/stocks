"""研究用一次性腳本：測試使用者提議的長期regime濾網(require_long_regime：額外要求
regime_fast_period(60)日均線>regime_slow_period(120)日均線才能進場，跟long_swing同一套
判斷)，套用在chip_momentum/trust_momentum/golden_cross/atr_breakout/breakout
這5支目前缺長期趨勢確認的策略上。起因是使用者質疑這些策略在股票見頂反轉後會「一路撿一路
停損」——2314(台揚，2021-08見頂)、4763(材料*-KY，2023-08見頂)這兩支新加入觀察清單的
long-term下跌股就是實例，見頂後這5支策略的獲利因子都掉到1以下(golden_cross在
2314上-130.9%、chip_momentum-107.8%)，只有長期regime濾網(60/120日均線)的long_swing
撐得住。全觀察清單10年+2314/4763見頂後窗口回測比較。不動STRATEGY_REGISTRY的預設params。"""
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
from stocks.strategies.atr_breakout import ATRBreakoutStrategy
from stocks.strategies.breakout import BreakoutStrategy
from stocks.strategies.chip_momentum import ChipMomentumStrategy
from stocks.strategies.golden_cross import GoldenCrossStrategy
from stocks.strategies.trust_momentum import TrustMomentumStrategy
from stocks.strategy_stats import is_scaleout_strategy, simulate_round_trips, simulate_scaleout_trades, summarize_trades

STRATEGIES = {
    "chip_momentum": ChipMomentumStrategy(),
    "trust_momentum": TrustMomentumStrategy(),
    "golden_cross": GoldenCrossStrategy(),
    "atr_breakout": ATRBreakoutStrategy(),
    "breakout": BreakoutStrategy(),
}
NEEDS_FLOWS = {"chip_momentum": "foreign_net", "trust_momentum": "trust_net", "golden_cross": None}
PEAK_WINDOWS = {"2314": pd.Timestamp("2021-08-01"), "4763": pd.Timestamp("2023-08-01")}


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


def run_trades(strategy_name, strategy_obj, bars, params, start=None, end=None):
    required_col = NEEDS_FLOWS.get(strategy_name)
    if required_col and required_col not in bars.columns:
        return []
    events = strategy_obj.evaluate(bars.attrs.get("symbol", ""), bars, params)
    if start is not None:
        events = [e for e in events if e.ts >= start]
    if end is not None:
        events = [e for e in events if e.ts <= end]
    trades, _ = (
        simulate_scaleout_trades(events) if is_scaleout_strategy(strategy_name, params) else simulate_round_trips(events)
    )
    return trades


def main():
    config = load_config()
    with connect(config.db_path) as conn:
        symbols = [(row["code"], row["name"]) for row in fetch_watchlist(conn)]
        bars_by_symbol = {}
        for code, name in symbols:
            bars = bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date")
            bars = attach_institutional_flows(bars, fetch_institutional_flows(conn, code))
            bars.attrs["symbol"] = code
            bars_by_symbol[code] = bars

    for strategy_name, strategy_obj in STRATEGIES.items():
        base_params = config.strategy_params[strategy_name]
        configs = [("現行(無regime濾網)", base_params), ("+長期regime濾網(60/120日均線)", {**base_params, "require_long_regime": True})]

        print(f"\n{'=' * 20} {strategy_name} {'=' * 20}")

        rows = []
        for label, extra in configs:
            all_trades = []
            for code, name in symbols:
                bars = bars_by_symbol[code]
                if bars.empty:
                    continue
                all_trades.extend(run_trades(strategy_name, strategy_obj, bars, extra))
            rows.append({"設定": label, **summarize(all_trades)})
        print("\n--- 全觀察清單10年 ---")
        print(pd.DataFrame(rows).to_string(index=False))

        print("\n--- 2314/4763 見頂後逐檔明細 ---")
        focus_rows = []
        for label, extra in configs:
            for code, peak_start in PEAK_WINDOWS.items():
                bars = bars_by_symbol.get(code)
                if bars is None or bars.empty:
                    continue
                trades = run_trades(strategy_name, strategy_obj, bars, extra, start=peak_start)
                focus_rows.append({"代號": code, "設定": label, **summarize(trades)})
        print(pd.DataFrame(focus_rows).to_string(index=False))


if __name__ == "__main__":
    main()
