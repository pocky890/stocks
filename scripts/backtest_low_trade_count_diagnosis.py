"""使用者接著問「很多策略在某隻股票也都剩十多筆或<10筆，也查一下」——延續atr_breakout
筆數診斷，這裡對全觀察清單、9支NOTIFIABLE_STRATEGIES的每個(策略,股票)組合，只挑目前
「有效保留」(should_disable()==False，也就是會實際推播/計入模擬交易的組合)裡樣本數
偏低(<15筆)的，逐一診斷原因：是這支股票資料庫歷史本來就短(新增沒多久)，還是這支股票
歷史夠長但這支策略在它身上訊號本來就稀疏(濾網疊加或股性使然)。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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
from stocks.notifier import NOTIFIABLE_STRATEGIES
from stocks.strategy_selection import MIN_TRADES_FOR_RANKING, should_disable, summarize_strategy


def main():
    config = load_config()
    with connect(config.db_path) as conn:
        symbols = [(row["code"], row["name"]) for row in fetch_watchlist(conn)]
        bars_by_symbol = {}
        history_years = {}
        for code, name in symbols:
            bars = bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date")
            if bars.empty:
                continue
            history_years[code] = (bars.index[-1] - bars.index[0]).days / 365.25
            bars = attach_institutional_flows(bars, fetch_institutional_flows(conn, code))
            bars = attach_monthly_revenue_growth(bars, [dict(r) for r in fetch_monthly_revenue(conn, code)])
            bars_by_symbol[code] = bars

    kept_low_n = []
    kept_total = 0
    for code, name in symbols:
        bars = bars_by_symbol.get(code)
        if bars is None:
            continue
        for strategy_name in sorted(NOTIFIABLE_STRATEGIES):
            summary = summarize_strategy(code, bars, strategy_name, config.strategy_params.get(strategy_name, {}))
            if should_disable(summary):
                continue
            kept_total += 1
            if summary["n"] < 15:
                kept_low_n.append((strategy_name, code, name, summary, history_years[code]))

    print(f"目前有效保留(不在排除清單)的組合共{kept_total}個，其中樣本<15筆的有{len(kept_low_n)}個：\n")
    kept_low_n.sort(key=lambda x: x[3]["n"])
    for strategy_name, code, name, s, years in kept_low_n:
        pf = s.get("profit_factor")
        pf_text = f"{pf:.2f}" if pf is not None else "∞"
        print(
            f"  {strategy_name:22s} {code} {name:6s} n={s['n']:3d} 勝率={s['win_rate']:5.1f}% "
            f"平均={s['avg_return_pct']:+6.1f}% 加總={s['total_return_pct']:+7.1f}% 獲利因子={pf_text:>6s} "
            f"股票歷史={years:4.1f}年"
        )

    print(f"\n<10筆: {sum(1 for *_, s, _ in kept_low_n if s['n'] < 10)}個　"
          f"10~14筆: {sum(1 for *_, s, _ in kept_low_n if 10 <= s['n'] < 15)}個")
    print(f"\n按策略統計樣本<15筆的組合數：")
    from collections import Counter
    counts = Counter(row[0] for row in kept_low_n)
    for name, cnt in counts.most_common():
        print(f"  {name}: {cnt}個")


if __name__ == "__main__":
    main()
