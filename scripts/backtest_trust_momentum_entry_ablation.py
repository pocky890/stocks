"""使用者看dashboard的trust_momentum「策略歷史勝率參考」表格，質疑「先不管要不要保留，
看這個筆數就很不合理，十年只交易了個位數？」——延續atr_breakout那次的診斷方法，對
trust_momentum做同樣的進場濾網ablation(固定現行stop_pct=0.20)，量化require_long_
regime/require_revenue_growth各自砍掉多少筆，同時看清楚trust_momentum entry_mode=
"window10_3"這個進場條件(近15日累積買超為正+近3日內至少2日買超)本身是level-triggered、
一次進場後要抱到停損/爆量出貨賣完才會有下一次交易，不是「訊號很少見」，是「一次進場
抱很久，同一段10年時間裡能跑出的完整買賣配對次數自然有限」的機制，要分清楚是這個
機制性原因還是濾網疊加把訊號濾掉了。全觀察清單10年。"""
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
from stocks.strategies import STRATEGY_REGISTRY
from stocks.strategy_stats import is_scaleout_strategy, simulate_round_trips, simulate_scaleout_trades, summarize_trades


def summarize(trades):
    s = summarize_trades(trades)
    if s is None:
        return {"筆數": 0, "勝率": None, "加總報酬": 0.0, "獲利因子": None, "最大回撤": 0.0}
    return {
        "筆數": s["n"],
        "勝率": round(s["win_rate"], 1),
        "加總報酬": round(s["total_return_pct"], 1),
        "獲利因子": round(s["profit_factor"], 2) if s["profit_factor"] is not None else None,
        "最大回撤": round(-s["max_drawdown_pct"], 1),
    }


def run(bars_by_symbol, params):
    strategy_obj = STRATEGY_REGISTRY["trust_momentum"]
    scaleout = is_scaleout_strategy("trust_momentum", params)
    all_trades = []
    entry_days = 0
    for code, bars in bars_by_symbol.items():
        events = strategy_obj.evaluate(code, bars, params)
        buy_events = [e for e in events if e.direction.name == "BUY"]
        entry_days += len(buy_events)
        trades, _ = simulate_scaleout_trades(events) if scaleout else simulate_round_trips(events)
        all_trades.extend(trades)
    result = summarize(all_trades)
    result["BUY事件數"] = entry_days
    return result


def main():
    config = load_config()
    with connect(config.db_path) as conn:
        symbols = [(row["code"], row["name"]) for row in fetch_watchlist(conn)]
        bars_by_symbol = {}
        for code, name in symbols:
            bars = bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date")
            if bars.empty:
                continue
            bars = attach_institutional_flows(bars, fetch_institutional_flows(conn, code))
            bars = attach_monthly_revenue_growth(bars, [dict(r) for r in fetch_monthly_revenue(conn, code)])
            bars_by_symbol[code] = bars
    print(f"全觀察清單: {len(bars_by_symbol)}檔\n")

    base = dict(config.strategy_params["trust_momentum"])  # 現行(stop_pct=0.20)

    def variant(**overrides):
        p = dict(base)
        p.update(overrides)
        return p

    print("=" * 20 + " 進場濾網疊加式ablation(固定stop_pct=0.20) " + "=" * 20)
    steps = [
        ("完全不設進場濾網(只留投信買超訊號)", variant(require_long_regime=False, require_revenue_growth=False)),
        ("+60/120regime", variant(require_long_regime=True, require_revenue_growth=False)),
        ("+月營收年增率(現行兩項全開)", variant(require_long_regime=True, require_revenue_growth=True)),
    ]
    rows = [{"設定": label, **run(bars_by_symbol, params)} for label, params in steps]
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n" + "=" * 20 + " 停損寬度本身對筆數的獨立影響(現行兩項濾網固定) " + "=" * 20)
    stop_steps = [
        ("stop_pct=0.15(原本)", variant(stop_pct=0.15)),
        ("stop_pct=0.20(現行)", variant(stop_pct=0.20)),
        ("stop_pct=0.25", variant(stop_pct=0.25)),
    ]
    rows2 = [{"設定": label, **run(bars_by_symbol, params)} for label, params in stop_steps]
    print(pd.DataFrame(rows2).to_string(index=False))


if __name__ == "__main__":
    main()
