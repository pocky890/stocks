"""每支觀察清單股票各自backtest一次NOTIFIABLE_STRATEGIES的每個策略，勝率明顯偏弱的
就排掉(存進symbols.disabled_strategies)，run_live.py/run_batch.py之後幫這支股票評估
訊號時會跳過這些策略，不會再通知/寫進signal_events。dashboard的回測比較、建議買進、
策略歷史勝率頁面完全不受影響，還是照樣顯示全部策略供參考——排除只影響「會不會實際
通知」，不影響「能不能看到分析」。

不是自動選一個「最強的」，是排掉「明顯不夠好」的：市場狀態會變，選單一個贏家全押的
風險是贏家換人時卻還押著舊贏家；排除法留著大部分策略繼續跑，只是拉掉墊底的，比較保守。
2026-08-08使用者確認：樣本不夠(交易次數太少，包括完全沒有完整買賣配對)的策略也排除，
不給預設保留的寬限期——那個勝率本身就不可信，不可信就該保守排除，不該拿雜訊當依據
推播通知。新股票/新啟用的策略會先整組安靜，等資料累積夠了下次重跑才會開始有判斷結果。

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
from stocks.strategy_selection import (
    MIN_AVG_RETURN_PCT,
    MIN_PROFIT_FACTOR,
    MIN_TOTAL_RETURN_PCT,
    MIN_TRADES_FOR_RANKING,
    MIN_TRADES_OVERRIDES,
    should_disable,
    summarize_strategy,
)


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
            disable = should_disable(summary, strategy_name)  # 每種情況都統一交給should_disable判斷，不要各自分支硬寫
            min_trades = MIN_TRADES_OVERRIDES.get(strategy_name, MIN_TRADES_FOR_RANKING)

            if not summary:
                print(f"  {strategy_name}: 沒有完整的買賣配對 -> {'排除' if disable else '保留'}")
            elif summary["n"] < min_trades:
                print(f"  {strategy_name}: {summary['n']}筆(樣本太少) -> {'排除' if disable else '保留'}")
            else:
                excl_best = summary.get("avg_return_excluding_best_pct")
                excl_best_text = f"，拿掉最佳單筆後{excl_best:+.1f}%" if excl_best is not None else ""
                pf = summary["profit_factor"]
                pf_text = f"{pf:.1f}" if pf is not None else "∞(無虧損)"
                print(
                    f"  {strategy_name}: {summary['n']}筆，勝率{summary['win_rate']:.0f}%，"
                    f"平均{summary['avg_return_pct']:+.1f}%，加總{summary['total_return_pct']:+.1f}%，"
                    f"獲利因子{pf_text}{excl_best_text} -> {'排除' if disable else '保留'}"
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
        f"完成。門檻: 交易數<{MIN_TRADES_FOR_RANKING}筆(樣本不足，含完全沒有完整買賣配對)就排除；"
        f"樣本足夠時平均報酬率<{MIN_AVG_RETURN_PCT:+.1f}%、加總報酬<={MIN_TOTAL_RETURN_PCT:+.1f}%、"
        f"或獲利因子<{MIN_PROFIT_FACTOR:.1f}，任一項沒過就排除(不因為報酬集中在少數大波段就排除，"
        "那是趨勢跟隨策略的正常樣貌；不單獨用MDD當門檻，MDD深但獲利因子夠高代表過程顛簸但賺賠比紮實)。"
        "建議每隔一段時間(例如每月)重跑一次，隨資料累積讓樣本不足的策略開始被真正判斷。"
    )


if __name__ == "__main__":
    main()
