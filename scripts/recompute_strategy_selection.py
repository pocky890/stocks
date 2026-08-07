"""每支觀察清單股票各自backtest一次NOTIFIABLE_STRATEGIES的每個策略，勝率明顯偏弱的
就排掉(存進symbols.disabled_strategies)，run_live.py/run_batch.py之後幫這支股票評估
訊號時會跳過這些策略，不會再通知/寫進signal_events。dashboard的回測比較、建議買進、
策略歷史勝率頁面完全不受影響，還是照樣顯示全部策略供參考——排除只影響「會不會實際
通知」，不影響「能不能看到分析」。

不是自動選一個「最強的」，是排掉「明顯不夠好」的：市場狀態會變，選單一個贏家全押的
風險是贏家換人時卻還押著舊贏家；排除法留著大部分策略繼續跑，只是拉掉墊底的，比較保守。
也不是每次都硬選幾個排除——樣本不夠(交易次數太少)的策略不排除，因為那個勝率本身
就不可信，排除它反而是憑噪音做決定。

手動執行，建議每隔一段時間(例如每月)重跑一次，隨著累積更多歷史資料更新排除清單。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stocks.config import load_config
from stocks.db import (
    attach_institutional_flows,
    bars_to_dataframe,
    connect,
    fetch_bars_daily,
    fetch_institutional_flows,
    fetch_watchlist,
    get_disabled_strategies,
    set_disabled_strategies,
)
from stocks.notifier import NOTIFIABLE_STRATEGIES
from stocks.strategy_selection import MIN_AVG_RETURN_PCT, MIN_TRADES_FOR_RANKING, WIN_RATE_THRESHOLD, should_disable, summarize_strategy


def main():
    config = load_config()

    with connect(config.db_path) as conn:
        watchlist = fetch_watchlist(conn)

    print(f"觀察清單: {[row['code'] for row in watchlist]}\n")

    for row in watchlist:
        symbol, name = row["code"], row["name"]
        with connect(config.db_path) as conn:
            bars = bars_to_dataframe(fetch_bars_daily(conn, symbol), ts_field="date")
            if bars.empty:
                print(f"{symbol} {name}: 沒有日線資料，跳過")
                continue
            bars = attach_institutional_flows(bars, fetch_institutional_flows(conn, symbol))
            previously_disabled = get_disabled_strategies(conn, symbol)

        print(f"=== {symbol} {name} ===")
        newly_disabled = []
        for strategy_name in sorted(NOTIFIABLE_STRATEGIES):
            summary = summarize_strategy(symbol, bars, strategy_name, config.strategy_params.get(strategy_name, {}))
            if not summary:
                print(f"  {strategy_name}: 沒有完整的買賣配對")
                continue
            if summary["n"] < MIN_TRADES_FOR_RANKING:
                print(f"  {strategy_name}: {summary['n']}筆(樣本太少，不排除)，勝率{summary['win_rate']:.0f}%")
                continue

            disable = should_disable(summary)
            print(
                f"  {strategy_name}: {summary['n']}筆，勝率{summary['win_rate']:.0f}%，"
                f"平均{summary['avg_return_pct']:+.1f}% -> {'排除' if disable else '保留'}"
            )
            if disable:
                newly_disabled.append(strategy_name)

        if set(newly_disabled) != set(previously_disabled):
            with connect(config.db_path) as conn:
                set_disabled_strategies(conn, symbol, newly_disabled)
            print(f"  => 更新排除清單: {newly_disabled or '(無)'}（原本: {previously_disabled or '(無)'}）")
        else:
            print(f"  => 排除清單沒有變化: {newly_disabled or '(無)'}")
        print()

    print(
        f"完成。門檻: 交易數>={MIN_TRADES_FOR_RANKING}筆時，勝率<{WIN_RATE_THRESHOLD:.0f}%或"
        f"平均報酬率<{MIN_AVG_RETURN_PCT:+.1f}%(任一觸發)就排除；樣本不足或雙項都達標的策略"
        "維持照常運作。建議每隔一段時間(例如每月)重跑一次。"
    )


if __name__ == "__main__":
    main()
