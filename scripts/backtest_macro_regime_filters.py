"""研究用一次性腳本：驗證使用者轉述Gemini建議的3個「總體位階濾網(Macro Regime Filter)」
提案——(1)120日均線斜率向上，套用在抄底類策略(bullish_divergence/capitulation_reversal)
上，區分「長線仍多頭、只是短線跌深」vs「結構性空頭裡的死貓反彈」；(2)52週高點回撤>40%
擋新BUY，套用在上一輪基本面濾網測起來效果不好的chip_momentum/long_swing上；(3)收盤價
>240日均線(年線)的絕對位階濾網，跟現有require_long_regime(60/120日均線交叉)比較，套用在
chip_momentum/trust_momentum/golden_cross_scaleout/atr_breakout/breakout這5支已有
regime濾網的策略上。全觀察清單10年+「20支已知近年跌很兇的股票」(2314/4763+2026-08-16
新加的18支，00664R反向ETF排除)兩個範圍比較。不動STRATEGY_REGISTRY的預設params，也不
修改任何策略檔案的預設值(全部是新增的研究參數，預設False)。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd

from stocks.config import load_config
from stocks.db import attach_institutional_flows, bars_to_dataframe, connect, fetch_bars_daily, fetch_institutional_flows, fetch_watchlist
from stocks.strategies import STRATEGY_REGISTRY
from stocks.strategy_stats import is_scaleout_strategy, simulate_round_trips, simulate_scaleout_trades, summarize_trades

KNOWN_DECLINERS = {
    "2314", "4763", "8444", "2929", "4426", "8437", "4174", "8044", "1340", "2239",
    "3552", "4529", "8429", "2726", "1338", "1565", "4552", "4416", "8450",
}  # 00664R(反向ETF)排除，理由同scripts/backtest_revenue_growth_filter.py


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


def run_scope(strategy_name, strategy_obj, configs, bars_by_symbol, symbols, code_filter):
    rows = []
    for label, extra in configs:
        scaleout = is_scaleout_strategy(strategy_name, extra)
        all_trades = []
        for code, name in symbols:
            if not code_filter(code):
                continue
            bars = bars_by_symbol[code]
            if bars.empty:
                continue
            events = strategy_obj.evaluate(code, bars, extra)
            trades, _ = simulate_scaleout_trades(events) if scaleout else simulate_round_trips(events)
            all_trades.extend(trades)
        rows.append({"設定": label, **summarize(all_trades)})
    return rows


def run_strategy(strategy_name, configs, config, bars_by_symbol, symbols):
    strategy_obj = STRATEGY_REGISTRY[strategy_name]
    print(f"\n{'=' * 20} {strategy_name} {'=' * 20}")
    for scope_label, code_filter in [("全觀察清單10年", lambda c: True), ("20支已知近年下跌很兇的股票", lambda c: c in KNOWN_DECLINERS)]:
        rows = run_scope(strategy_name, strategy_obj, configs, bars_by_symbol, symbols, code_filter)
        print(f"\n--- {scope_label} ---")
        print(pd.DataFrame(rows).to_string(index=False))


def main():
    config = load_config()
    with connect(config.db_path) as conn:
        symbols = [(row["code"], row["name"]) for row in fetch_watchlist(conn)]
        bars_by_symbol = {}
        for code, name in symbols:
            bars = bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date")
            bars_by_symbol[code] = attach_institutional_flows(bars, fetch_institutional_flows(conn, code))

    print("\n\n########## (1) 120日均線斜率濾網 -- 抄底類策略 ##########")
    for strategy_name in ["bullish_divergence", "capitulation_reversal"]:
        base_params = config.strategy_params[strategy_name]
        configs = [
            ("現行(無位階濾網)", base_params),
            ("+120MA斜率向上", {**base_params, "require_long_uptrend_intact": True}),
        ]
        run_strategy(strategy_name, configs, config, bars_by_symbol, symbols)

    print("\n\n########## (2) 52週高點回撤>40%濾網 -- chip_momentum/long_swing ##########")
    for strategy_name in ["chip_momentum", "long_swing"]:
        base_params = config.strategy_params[strategy_name]
        configs = [
            ("現行(已有regime濾網)", base_params),
            ("+52週高點回撤限制40%", {**base_params, "require_within_drawdown_limit": True}),
            (
                "+52週高點回撤限制40%+MA240",
                {**base_params, "require_within_drawdown_limit": True, "require_above_long_ma": True},
            ),
        ]
        run_strategy(strategy_name, configs, config, bars_by_symbol, symbols)

    print("\n\n########## (3) MA240年線濾網 vs 現有60/120日regime濾網 ##########")
    for strategy_name in ["chip_momentum", "trust_momentum", "golden_cross_scaleout", "atr_breakout", "breakout"]:
        base_params = config.strategy_params[strategy_name]
        no_regime_params = {**base_params, "require_long_regime": False}
        configs = [
            ("現行(60/120日regime濾網)", base_params),
            ("只用MA240年線(不用60/120regime)", {**no_regime_params, "require_above_long_ma": True}),
            ("60/120regime+MA240都要", {**base_params, "require_above_long_ma": True}),
        ]
        run_strategy(strategy_name, configs, config, bars_by_symbol, symbols)


if __name__ == "__main__":
    main()
