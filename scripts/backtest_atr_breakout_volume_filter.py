"""研究用一次性腳本：測試使用者提議的atr_breakout(ATR動態通道突破)進場量能濾網
(require_entry_volume：突破當天成交量>volume_multiplier倍均量，跟姊妹策略breakout同一套
命名)——全觀察清單10年+2026 YTD回測比較，並列出8299/2313 YTD表現。不動STRATEGY_REGISTRY
的預設params。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd

from stocks.config import load_config
from stocks.db import bars_to_dataframe, connect, fetch_bars_daily, fetch_watchlist
from stocks.strategies.atr_breakout import ATRBreakoutStrategy
from stocks.strategy_stats import simulate_round_trips, summarize_trades

YTD_START = pd.Timestamp("2026-01-01")
FOCUS_SYMBOLS = {"8299", "2313"}


def build_configs(base_params):
    return [
        ("現行config.json", base_params),
        ("+進場量能1.3倍(同breakout命名)", {**base_params, "require_entry_volume": True, "volume_multiplier": 1.3}),
        ("+進場量能1.5倍(同breakout命名)", {**base_params, "require_entry_volume": True, "volume_multiplier": 1.5}),
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
    strategy = ATRBreakoutStrategy()
    base_params = config.strategy_params["atr_breakout"]
    configs = build_configs(base_params)

    with connect(config.db_path) as conn:
        symbols = [(row["code"], row["name"]) for row in fetch_watchlist(conn)]
        bars_by_symbol = {code: bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date") for code, _ in symbols}

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
                trades, _ = simulate_round_trips(events)
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
            trades, _ = simulate_round_trips(events)
            focus_rows.append({"代號": code, "名稱": name, "設定": label, **summarize(trades)})
    print(pd.DataFrame(focus_rows).to_string(index=False))


if __name__ == "__main__":
    main()
