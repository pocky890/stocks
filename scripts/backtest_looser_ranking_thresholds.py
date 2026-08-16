"""研究用一次性腳本：使用者轉述Gemini建議「把排除門檻放寬為平均報酬>2% AND 獲利因子>1.5」
(現行:strategy_selection.py的MIN_AVG_RETURN_PCT=4.0、MIN_PROFIT_FACTOR=2.0)，理由是
「這個區間內的策略上線實戰時績效最不容易變形(robust)」。這個說法本身沒有針對這份資料集
驗證過，不能只憑「聽起來合理」就採用——這裡直接算出全觀察清單目前每個(策略,股票)組合
的summarize_strategy()，比較現行門檻 vs 放寬門檻(MIN_TOTAL_RETURN_PCT/MIN_TRADES_FOR_
RANKING維持不變，只動平均報酬跟獲利因子這兩個)，列出「原本排除、放寬後變成保留」的
組合清單本身，直接檢視這些新增組合是不是真的看起來穩健，而不是又找到一批雜訊案例
(跟之前MIN_TRADES_OVERRIDES那次「查證3組單筆巧合」是同一種查證方式)。"""
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
from stocks.strategy_selection import MIN_TOTAL_RETURN_PCT, MIN_TRADES_FOR_RANKING, summarize_strategy

CURRENT_MIN_AVG_RETURN_PCT = 4.0
CURRENT_MIN_PROFIT_FACTOR = 2.0
LOOSER_MIN_AVG_RETURN_PCT = 2.0
LOOSER_MIN_PROFIT_FACTOR = 1.5


def would_disable(summary, min_avg_return, min_profit_factor):
    if not summary or summary["n"] < MIN_TRADES_FOR_RANKING:
        return True
    if summary["avg_return_pct"] < min_avg_return:
        return True
    if summary["total_return_pct"] <= MIN_TOTAL_RETURN_PCT:
        return True
    pf = summary.get("profit_factor")
    return pf is not None and pf < min_profit_factor


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

    newly_included = []
    still_excluded_but_closer = []
    for code, name in symbols:
        bars = bars_by_symbol.get(code)
        if bars is None:
            continue
        for strategy_name in sorted(NOTIFIABLE_STRATEGIES):
            summary = summarize_strategy(code, bars, strategy_name, config.strategy_params.get(strategy_name, {}))
            disabled_now = would_disable(summary, CURRENT_MIN_AVG_RETURN_PCT, CURRENT_MIN_PROFIT_FACTOR)
            disabled_looser = would_disable(summary, LOOSER_MIN_AVG_RETURN_PCT, LOOSER_MIN_PROFIT_FACTOR)
            if disabled_now and not disabled_looser:
                newly_included.append((strategy_name, code, name, summary))

    print(f"現行門檻(平均報酬>{CURRENT_MIN_AVG_RETURN_PCT}%、獲利因子>{CURRENT_MIN_PROFIT_FACTOR}) "
          f"排除、放寬後(平均報酬>{LOOSER_MIN_AVG_RETURN_PCT}%、獲利因子>{LOOSER_MIN_PROFIT_FACTOR}) "
          f"會變成保留的組合，共{len(newly_included)}個：\n")
    newly_included.sort(key=lambda x: x[3]["n"])
    for strategy_name, code, name, s in newly_included:
        pf = s.get("profit_factor")
        pf_text = f"{pf:.2f}" if pf is not None else "∞"
        print(
            f"  {strategy_name:22s} {code} {name:6s} n={s['n']:3d} 勝率={s['win_rate']:5.1f}% "
            f"平均={s['avg_return_pct']:+6.1f}% 加總={s['total_return_pct']:+7.1f}% 獲利因子={pf_text}"
        )


if __name__ == "__main__":
    main()
