"""研究用一次性腳本：測試使用者(轉述Gemini建議)對chip_momentum(外資買超動能)/
trust_momentum(投信買超動能)提出的兩個量能濾網——(1)進場加量能確認(require_entry_volume，
"帶量點火")、(2)出場加爆量出貨警示(stop_mode="volume_alert_scaleout"，高檔跌破5MA/10MA
且爆量時先賣一半)——全觀察清單10年+2026 YTD回測比較，並額外列出8299/2313這兩支2026年
虧損個股的YTD表現有沒有改善。不動STRATEGY_REGISTRY的預設params。"""
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
from stocks.strategies.chip_momentum import ChipMomentumStrategy
from stocks.strategies.trust_momentum import TrustMomentumStrategy
from stocks.strategy_stats import is_scaleout_strategy, simulate_round_trips, simulate_scaleout_trades, summarize_trades

YTD_START = pd.Timestamp("2026-01-01")
FOCUS_SYMBOLS = {"8299", "2313"}


def build_configs(base_params):
    return [
        ("現行config.json", base_params),
        ("+進場帶量1.3倍", {**base_params, "require_entry_volume": True, "entry_volume_multiplier": 1.3}),
        ("+進場帶量1.5倍", {**base_params, "require_entry_volume": True, "entry_volume_multiplier": 1.5}),
        ("+出場爆量警示(5MA)", {**base_params, "stop_mode": "volume_alert_scaleout", "alert_ma_period": 5}),
        ("+出場爆量警示(10MA)", {**base_params, "stop_mode": "volume_alert_scaleout", "alert_ma_period": 10}),
        (
            "+進場帶量1.5倍+出場爆量警示(5MA)",
            {
                **base_params,
                "require_entry_volume": True,
                "entry_volume_multiplier": 1.5,
                "stop_mode": "volume_alert_scaleout",
                "alert_ma_period": 5,
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


def run_for_strategy(strategy_name, strategy_obj, config, bars_by_symbol, symbols):
    base_params = config.strategy_params[strategy_name]
    configs = build_configs(base_params)
    required_col = "trust_net" if strategy_name == "trust_momentum" else "foreign_net"

    print(f"\n{'=' * 20} {strategy_name} {'=' * 20}")

    for scope_label, start in [("全觀察清單10年", None), ("2026 YTD", YTD_START)]:
        rows = []
        for label, extra in configs:
            all_trades = []
            for code, name in symbols:
                bars = bars_by_symbol[code]
                if bars.empty or required_col not in bars.columns:
                    continue
                events = strategy_obj.evaluate(code, bars, extra)
                if start is not None:
                    events = [e for e in events if e.ts >= start]
                trades, _ = (
                    simulate_scaleout_trades(events) if is_scaleout_strategy(strategy_name, extra) else simulate_round_trips(events)
                )
                all_trades.extend(trades)
            rows.append({"設定": label, **summarize(all_trades)})
        print(f"\n--- {scope_label} ---")
        print(pd.DataFrame(rows).to_string(index=False))

    print(f"\n--- 8299/2313 2026 YTD 逐檔明細 ---")
    focus_rows = []
    for label, extra in configs:
        for code, name in symbols:
            if code not in FOCUS_SYMBOLS:
                continue
            bars = bars_by_symbol[code]
            if bars.empty or required_col not in bars.columns:
                continue
            events = strategy_obj.evaluate(code, bars, extra)
            events = [e for e in events if e.ts >= YTD_START]
            trades, _ = (
                simulate_scaleout_trades(events) if is_scaleout_strategy(strategy_name, extra) else simulate_round_trips(events)
            )
            focus_rows.append({"代號": code, "名稱": name, "設定": label, **summarize(trades)})
    print(pd.DataFrame(focus_rows).to_string(index=False))


def main():
    config = load_config()
    with connect(config.db_path) as conn:
        symbols = [(row["code"], row["name"]) for row in fetch_watchlist(conn)]
        bars_by_symbol = {}
        for code, name in symbols:
            bars = bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date")
            bars_by_symbol[code] = attach_institutional_flows(bars, fetch_institutional_flows(conn, code))

    run_for_strategy("chip_momentum", ChipMomentumStrategy(), config, bars_by_symbol, symbols)
    run_for_strategy("trust_momentum", TrustMomentumStrategy(), config, bars_by_symbol, symbols)


if __name__ == "__main__":
    main()
