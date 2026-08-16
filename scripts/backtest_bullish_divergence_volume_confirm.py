"""研究用一次性腳本：測試使用者(轉述Gemini建議)對bullish_divergence(背離抄底)提出的量能
濾網——「創新低那天要求爆量(恐慌換手)」(require_capitulation_volume) 或 「確認訊號那天
要求爆量」(require_reversal_volume，跟require_reversal_kd/require_reversal_macd同一套
confirm_signals機制)——全觀察清單10年+2026 YTD回測比較，並列出8299/2313 YTD表現，
以及新濾網跟capitulation_reversal訊號的重疊天數(檢查兩支策略是否變得高度重複)。不動
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
from stocks.models import Direction
from stocks.strategies.bullish_divergence import BullishDivergenceStrategy
from stocks.strategies.capitulation_reversal import CapitulationReversalStrategy
from stocks.strategy_stats import is_scaleout_strategy, simulate_round_trips, simulate_scaleout_trades, summarize_trades

YTD_START = pd.Timestamp("2026-01-01")
FOCUS_SYMBOLS = {"8299", "2313"}


def build_configs(base_params):
    return [
        ("現行config.json", base_params),
        ("+創新低當天爆量1.5倍", {**base_params, "require_capitulation_volume": True, "capitulation_volume_multiplier": 1.5}),
        ("+確認訊號當天爆量1.5倍", {**base_params, "require_reversal_volume": True, "reversal_volume_multiplier": 1.5}),
        (
            "+創新低+確認訊號都要爆量",
            {
                **base_params,
                "require_capitulation_volume": True,
                "capitulation_volume_multiplier": 1.5,
                "require_reversal_volume": True,
                "reversal_volume_multiplier": 1.5,
            },
        ),
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


def main():
    config = load_config()
    strategy = BullishDivergenceStrategy()
    capitulation_strategy = CapitulationReversalStrategy()
    base_params = config.strategy_params["bullish_divergence"]
    capitulation_params = config.strategy_params["capitulation_reversal"]
    configs = build_configs(base_params)

    with connect(config.db_path) as conn:
        symbols = [(row["code"], row["name"]) for row in fetch_watchlist(conn)]
        bars_by_symbol = {}
        for code, name in symbols:
            bars = bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date")
            bars_by_symbol[code] = attach_institutional_flows(bars, fetch_institutional_flows(conn, code))

    for scope_label, start in [("全觀察清單10年", None), ("2026 YTD", YTD_START)]:
        rows = []
        for label, extra in configs:
            all_trades = []
            for code, name in symbols:
                bars = bars_by_symbol[code]
                if bars.empty:
                    continue
                events = strategy.evaluate(code, bars, extra)
                if start is not None:
                    events = [e for e in events if e.ts >= start]
                trades, _ = (
                    simulate_scaleout_trades(events) if is_scaleout_strategy("bullish_divergence", extra) else simulate_round_trips(events)
                )
                all_trades.extend(trades)
            rows.append({"設定": label, **summarize(all_trades)})
        print(f"\n=== {scope_label} ===")
        print(pd.DataFrame(rows).to_string(index=False))

    print("\n=== 8299/2313 2026 YTD 逐檔明細 ===")
    focus_rows = []
    for label, extra in configs:
        for code, name in symbols:
            if code not in FOCUS_SYMBOLS:
                continue
            bars = bars_by_symbol[code]
            events = strategy.evaluate(code, bars, extra)
            events = [e for e in events if e.ts >= YTD_START]
            trades, _ = (
                simulate_scaleout_trades(events) if is_scaleout_strategy("bullish_divergence", extra) else simulate_round_trips(events)
            )
            focus_rows.append({"代號": code, "名稱": name, "設定": label, **summarize(trades)})
    print(pd.DataFrame(focus_rows).to_string(index=False))

    print("\n=== 加爆量濾網後，跟capitulation_reversal的BUY訊號重疊天數(全觀察清單10年) ===")
    overlap_rows = []
    for label, extra in [c for c in configs if c[0] != "現行config.json"]:
        overlap_days = 0
        bd_total_buys = 0
        for code, name in symbols:
            bars = bars_by_symbol[code]
            if bars.empty:
                continue
            bd_buy_dates = {e.ts for e in strategy.evaluate(code, bars, extra) if e.direction == Direction.BUY}
            cr_buy_dates = {e.ts for e in capitulation_strategy.evaluate(code, bars, capitulation_params) if e.direction == Direction.BUY}
            bd_total_buys += len(bd_buy_dates)
            overlap_days += len(bd_buy_dates & cr_buy_dates)
        overlap_rows.append({"設定": label, "背離抄底BUY總筆數": bd_total_buys, "跟capitulation_reversal同一天BUY筆數": overlap_days})
    print(pd.DataFrame(overlap_rows).to_string(index=False))


if __name__ == "__main__":
    main()
