"""買/賣訊號公式(buy_formula/sell_formula)的逐筆進出模擬——跟backtest.py的訊號次數
統計不一樣，這裡實際配對每次買進訊號後最近的一次賣出訊號，算出每筆交易的報酬率、
勝率跟總報酬率。不寫入signal_events，純分析用。

模擬規則(單一持股、不重複進場)：
- 買進訊號觸發時如果手上沒有部位，才建立部位(價格用訊號當天的價格)
- 賣出訊號觸發時如果手上有部位，才平倉並算報酬率
- 已經有部位時再出現買進訊號，忽略(不加碼、不重複進場)
- 資料结束時還持有的部位，另外列出「尚未平倉」，不計入勝率/總報酬率(還沒真正實現損益)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stocks.config import load_config
from stocks.db import attach_institutional_flows, bars_to_dataframe, connect, fetch_bars_daily, fetch_institutional_flows, fetch_watchlist
from stocks.models import Direction
from stocks.strategies.composite_formula import BuyFormulaStrategy, SellFormulaStrategy


def simulate(symbol, bars, buy_params, sell_params):
    signals = sorted(
        BuyFormulaStrategy().evaluate(symbol, bars, buy_params) + SellFormulaStrategy().evaluate(symbol, bars, sell_params),
        key=lambda e: e.ts,
    )

    trades = []
    open_trade = None
    for e in signals:
        if e.direction == Direction.BUY and open_trade is None:
            open_trade = (e.ts, e.price)
        elif e.direction == Direction.SELL and open_trade is not None:
            buy_ts, buy_price = open_trade
            ret_pct = (e.price - buy_price) / buy_price * 100
            trades.append(
                {
                    "buy_date": buy_ts.strftime("%Y-%m-%d"),
                    "sell_date": e.ts.strftime("%Y-%m-%d"),
                    "buy_price": buy_price,
                    "sell_price": e.price,
                    "return_pct": ret_pct,
                }
            )
            open_trade = None

    return trades, open_trade


def main():
    config = load_config()
    buy_params = config.strategy_params.get("buy_formula", {})
    sell_params = config.strategy_params.get("sell_formula", {})

    with connect(config.db_path) as conn:
        watchlist = fetch_watchlist(conn)
        all_trades = {}
        still_open = {}
        for row in watchlist:
            symbol, name = row["code"], row["name"]
            bars = bars_to_dataframe(fetch_bars_daily(conn, symbol), ts_field="date")
            if bars.empty:
                continue
            bars = attach_institutional_flows(bars, fetch_institutional_flows(conn, symbol))
            trades, open_trade = simulate(symbol, bars, buy_params, sell_params)
            all_trades[(symbol, name)] = trades
            if open_trade:
                still_open[(symbol, name)] = open_trade

    print("=== 各股逐筆交易 ===")
    pooled_returns = []
    for (symbol, name), trades in all_trades.items():
        if not trades:
            print(f"\n{symbol} {name}: 過去一年沒有完整的買賣配對")
            continue
        compounded = 1.0
        wins = 0
        print(f"\n{symbol} {name}:")
        for t in trades:
            compounded *= 1 + t["return_pct"] / 100
            pooled_returns.append(t["return_pct"])
            if t["return_pct"] > 0:
                wins += 1
            print(
                f"  {t['buy_date']} 買進@{t['buy_price']:.1f} -> {t['sell_date']} 賣出@{t['sell_price']:.1f}"
                f"  報酬率 {t['return_pct']:+.1f}%"
            )
        win_rate = wins / len(trades) * 100
        print(f"  -- 這檔共{len(trades)}筆交易，勝率{win_rate:.0f}%，若依序把本金投入每一筆的累積報酬率(複利): {(compounded - 1) * 100:+.1f}%")

    if still_open:
        print("\n=== 目前還持有中(還沒出現賣出訊號，不計入下面的統計) ===")
        for (symbol, name), (buy_ts, buy_price) in still_open.items():
            print(f"  {symbol} {name}: {buy_ts.strftime('%Y-%m-%d')} 買進@{buy_price:.1f}，尚未平倉")

    print("\n=== 整體統計(全部股票的已平倉交易合併計算) ===")
    if not pooled_returns:
        print("過去一年整個觀察清單都沒有出現過完整的買賣配對，無法算勝率/總報酬率")
        return
    wins = sum(1 for r in pooled_returns if r > 0)
    print(f"總交易筆數: {len(pooled_returns)}")
    print(f"整體勝率: {wins / len(pooled_returns) * 100:.0f}% ({wins}/{len(pooled_returns)})")
    print(f"每筆平均報酬率: {sum(pooled_returns) / len(pooled_returns):+.1f}%")
    print(
        "註：以上是每檔股票各自獨立模擬(假設每檔股票分開撥一筆本金，不是共用同一筆本金"
        "輪流交易)，「整體勝率/平均報酬率」是把所有股票的交易筆數直接合併計算，不是"
        "單一資金池的年化報酬率——你的觀察清單只有~1年資料，樣本數也不多，這個結果拿來"
        "看「訊號抓到的方向對不對」比較有意義，還不到能拿來評估精確報酬率的統計量。"
    )


if __name__ == "__main__":
    main()
