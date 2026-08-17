"""分策略回測比較：每個NOTIFIABLE_STRATEGIES(進場+出場邏輯完整、可以直接依據行動的策略)
各自在觀察清單上跑一次逐筆進出模擬，讓使用者能比較「哪個策略比較好」。

用strategy_stats.py(跟dashboard「策略歷史勝率」同一套邏輯)算勝率/報酬率，不寫入
signal_events，純分析用。這幾個策略(atr_breakout/chip_momentum/trend_following/
breakout)都各自完整定義BUY+SELL，用同一套simulate_round_trips配對邏輯可以直接比較。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stocks.config import load_config
from stocks.db import attach_institutional_flows, bars_to_dataframe, connect, fetch_bars_daily, fetch_institutional_flows, fetch_watchlist
from stocks.strategies import STRATEGY_REGISTRY
from stocks.strategies.golden_cross import GoldenCrossStrategy
from stocks.strategy_stats import simulate_round_trips, simulate_scaleout_trades, summarize_trades

STRATEGY_GROUPS = {
    "ATR動態通道突破(atr_breakout)": ["atr_breakout"],
    "外資買超動能(chip_momentum)": ["chip_momentum"],
    "趨勢追蹤(trend_following)": ["trend_following"],
    "Breakout突破(breakout)": ["breakout"],
}


def compounded_return_pct(trades) -> float:
    compounded = 1.0
    for t in trades:
        compounded *= 1 + t.return_pct / 100
    return (compounded - 1) * 100


def main():
    config = load_config()

    with connect(config.db_path) as conn:
        watchlist = fetch_watchlist(conn)
        bars_by_symbol = {}
        for row in watchlist:
            bars = bars_to_dataframe(fetch_bars_daily(conn, row["code"]), ts_field="date")
            if bars.empty:
                continue
            bars_by_symbol[(row["code"], row["name"])] = attach_institutional_flows(
                bars, fetch_institutional_flows(conn, row["code"])
            )

    print("=== 各策略在觀察清單上的回測比較 ===\n")
    ranking = []
    for group_label, strategy_names in STRATEGY_GROUPS.items():
        print(f"--- {group_label} ---")
        pooled_trades = []
        for (symbol, name), bars in bars_by_symbol.items():
            events = []
            for strat_name in strategy_names:
                events += STRATEGY_REGISTRY[strat_name].evaluate(symbol, bars, config.strategy_params.get(strat_name, {}))
            trades, _ = simulate_round_trips(events)
            if not trades:
                print(f"  {symbol} {name}: 沒有完整的買賣配對(訊號沒觸發，或觸發了但配不成一組)")
                continue
            summary = summarize_trades(trades)
            pooled_trades += trades
            print(
                f"  {symbol} {name}: {summary['n']}筆，勝率{summary['win_rate']:.0f}%，"
                f"複利報酬{compounded_return_pct(trades):+.1f}%"
            )

        overall = summarize_trades(pooled_trades)
        if overall:
            print(f"  => 整體(合併{overall['n']}筆): 勝率{overall['win_rate']:.0f}%，平均每筆{overall['avg_return_pct']:+.1f}%")
            ranking.append((group_label, overall["n"], overall["win_rate"], overall["avg_return_pct"]))
        else:
            print("  => 整個觀察清單都沒有出現過完整的買賣配對")
        print()

    print("=== 策略排名(依整體勝率排序) ===")
    if not ranking:
        print("所有策略在整個觀察清單上都沒有出現過完整的買賣配對")
        return
    for label, n, win_rate, avg_return in sorted(ranking, key=lambda r: -r[2]):
        print(f"  {label}: {n}筆，勝率{win_rate:.0f}%，平均每筆{avg_return:+.1f}%")
    print(
        "\n註：每檔股票各自獨立模擬(不是共用同一筆本金輪流交易)，勝率/平均報酬率是把所有股票的"
        "交易筆數直接合併計算。筆數少的策略(尤其個位數)排名參考價值較低，容易受一兩筆極端交易"
        "左右——這只適合看「訊號抓到的方向對不對」的粗略比較，不是精確的策略評分。"
    )

    print("\n=== 均線黃金交叉+籌碼、分批出場(golden_cross)：另外報告，不放進上面的排名 ===")
    print("(進出場形狀跟上面幾個不一樣：一次進場配兩次出場，報酬率用兩次出場價的均價計算)\n")
    scaleout_params = config.strategy_params.get("golden_cross", {})
    pooled_scaleout_trades = []
    still_open_notes = []
    for (symbol, name), bars in bars_by_symbol.items():
        events = GoldenCrossStrategy().evaluate(symbol, bars, scaleout_params)
        trades, still_open = simulate_scaleout_trades(events)
        if trades:
            summary = summarize_trades(trades)
            print(
                f"  {symbol} {name}: {summary['n']}筆，勝率{summary['win_rate']:.0f}%，"
                f"複利報酬{compounded_return_pct(trades):+.1f}%"
            )
            for t in trades:
                print(
                    f"    {t.entry_ts.strftime('%Y-%m-%d')} 買進@{t.entry_price:.1f}"
                    f" -> 半倉@{t.exit1_price:.1f}({t.exit1_ts.strftime('%Y-%m-%d')})"
                    f" -> 剩餘@{t.exit2_price:.1f}({t.exit2_ts.strftime('%Y-%m-%d')})"
                    f"  均價出場報酬率 {t.return_pct:+.1f}%"
                )
            pooled_scaleout_trades += trades
        else:
            print(f"  {symbol} {name}: 沒有完整的買賣配對")
        if still_open:
            exits_done = len(still_open["exits"])
            still_open_notes.append(f"  {symbol} {name}: {still_open['entry'].ts.strftime('%Y-%m-%d')}買進@{still_open['entry'].price:.1f}，"
                                     f"目前{'已賣一半，剩餘一半還持有中' if exits_done == 1 else '還沒觸發任何出場，全倉持有中'}")

    if still_open_notes:
        print("\n  尚未完全平倉(不計入下面的統計):")
        for note in still_open_notes:
            print(note)

    overall = summarize_trades(pooled_scaleout_trades)
    if overall:
        print(f"\n  => 整體(合併{overall['n']}筆): 勝率{overall['win_rate']:.0f}%，平均每筆{overall['avg_return_pct']:+.1f}%")
    else:
        print("\n  整個觀察清單都沒有出現過完整的買賣配對")


if __name__ == "__main__":
    main()
