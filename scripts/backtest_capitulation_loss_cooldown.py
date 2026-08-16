"""研究用一次性腳本：使用者質疑120MA斜率濾網(require_long_uptrend_intact)全觀察清單
總報酬砍71%代價太大，要求找更精準的替代方案。這裡測試`loss_cooldown_days`——不是
全面性擋掉「120MA走平」的股票(連正常整理的健康股票也一起濾掉)，改成只有這支股票自己
上一次capitulation_reversal「真的觸發全部出場的結構停損」(恐慌沒有真的止穩)才進入
冷卻期，更精準地只針對「這支股票反覆抄底失敗」的情況，不影響其他正常運作的股票。全
觀察清單10年+20支已知近年下跌很兇的股票兩個範圍比較，跟120MA斜率濾網並列給使用者
選擇。不動STRATEGY_REGISTRY的預設params。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd

from stocks.config import load_config
from stocks.db import attach_institutional_flows, bars_to_dataframe, connect, fetch_bars_daily, fetch_institutional_flows, fetch_watchlist
from stocks.strategies.capitulation_reversal import CapitulationReversalStrategy
from stocks.strategy_stats import simulate_scaleout_trades, summarize_trades

KNOWN_DECLINERS = {
    "2314", "4763", "8444", "2929", "4426", "8437", "4174", "8044", "1340", "2239",
    "3552", "4529", "8429", "2726", "1338", "1565", "4552", "4416", "8450",
}


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
    strategy = CapitulationReversalStrategy()
    base_params = config.strategy_params["capitulation_reversal"]
    configs = [
        ("現行(無位階濾網)", base_params),
        ("+120MA斜率向上(全面性)", {**base_params, "require_long_uptrend_intact": True}),
        ("+失敗冷卻30天(只針對這支股票)", {**base_params, "loss_cooldown_days": 30}),
        ("+失敗冷卻60天", {**base_params, "loss_cooldown_days": 60}),
        ("+失敗冷卻90天", {**base_params, "loss_cooldown_days": 90}),
        ("+失敗冷卻180天", {**base_params, "loss_cooldown_days": 180}),
        ("+失敗冷卻365天", {**base_params, "loss_cooldown_days": 365}),
        (
            "+失敗冷卻60天+120MA斜率",
            {**base_params, "loss_cooldown_days": 60, "require_long_uptrend_intact": True},
        ),
    ]

    with connect(config.db_path) as conn:
        symbols = [(row["code"], row["name"]) for row in fetch_watchlist(conn)]
        bars_by_symbol = {}
        for code, name in symbols:
            bars = bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date")
            bars_by_symbol[code] = attach_institutional_flows(bars, fetch_institutional_flows(conn, code))

    for scope_label, code_filter in [("全觀察清單10年", lambda c: True), ("20支已知近年下跌很兇的股票", lambda c: c in KNOWN_DECLINERS)]:
        rows = []
        for label, extra in configs:
            all_trades = []
            for code, name in symbols:
                if not code_filter(code):
                    continue
                bars = bars_by_symbol[code]
                if bars.empty:
                    continue
                events = strategy.evaluate(code, bars, extra)
                trades, _ = simulate_scaleout_trades(events)
                all_trades.extend(trades)
            rows.append({"設定": label, **summarize(all_trades)})
        print(f"\n--- {scope_label} ---")
        print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
