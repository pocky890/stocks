"""補三大法人買賣超、融資融券餘額、估值(PE/殖利率/PB)、除權息預告表——全部是免費公開資料，
跟Shioaji帳戶/金鑰狀態無關，現在就能跑。

三大法人/融資融券/估值的歷史回補全部改用FinMind(一支股票一次查詢就拿到整段歷史範圍)，
不分上市/上櫃——2026-08-17發現原本上市用TWSE官方API逐日查詢，10年歷史要打近2500次、
途中必然會遇到逾時；FinMind一支股票一次呼叫就拿完，上市上櫃都適用(上櫃官方API本來就
做不到逐日回補，只能靠FinMind)。每日例行更新(daily_update.py)維持用官方TWSE/TPEx API
追蹤「今天」的新資料，跟這裡的historical backfill是分開的兩條路，不受這裡改動影響——
但這裡跑完仍然要mark_market_data_synced，這樣daily_update.py的TWSE增量路徑才知道這段
歷史已經有人補過，不會每次開dashboard都想重新逐日補一次。

除權息預告表沒有FinMind可用的對應dataset，繼續用twse_client(只有上市；這是「目前排定中」
的清單，不是歷史資料，跟這裡其他三項backfill的性質不同)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stocks.config import load_config
from stocks.db import (
    connect,
    fetch_trading_dates,
    fetch_watchlist,
    init_db,
    insert_ex_dividend_schedule,
    insert_institutional_flows,
    insert_margin_balances,
    insert_valuations,
    mark_market_data_synced,
)
from stocks.finmind_client import (
    fetch_institutional_flows_for_range,
    fetch_margin_balances_for_range,
    fetch_valuations_for_range,
)
from stocks.twse_client import fetch_ex_dividend_schedule


def main():
    config = load_config()
    init_db(config.db_path)

    with connect(config.db_path) as conn:
        all_dates = fetch_trading_dates(conn)
        watchlist_rows = fetch_watchlist(conn)

    if not all_dates:
        print("bars_daily是空的，先跑 scripts/fetch_historical.py")
        return
    if not watchlist_rows:
        print("觀察清單是空的，先跑 scripts/fetch_historical.py")
        return

    start_date, end_date = all_dates[0], all_dates[-1]
    symbols = sorted(r["code"] for r in watchlist_rows)
    print(f"觀察清單({len(symbols)}檔): {symbols}")
    print(f"用FinMind補三大法人/融資融券/估值歷史 {start_date}~{end_date}...")

    for symbol in symbols:
        flows = fetch_institutional_flows_for_range(symbol, start_date, end_date)
        margins = fetch_margin_balances_for_range(symbol, start_date, end_date)
        valuations = fetch_valuations_for_range(symbol, start_date, end_date)
        with connect(config.db_path) as conn:
            if flows:
                insert_institutional_flows(conn, flows)
            if margins:
                insert_margin_balances(conn, margins)
            if valuations:
                insert_valuations(conn, valuations)
        print(f"  {symbol}: 籌碼{len(flows)}筆 / 融資融券{len(margins)}筆 / 估值{len(valuations)}筆")

    with connect(config.db_path) as conn:
        mark_market_data_synced(conn, all_dates)

    twse_symbols = {r["code"] for r in watchlist_rows if r["market"] != "TPEx"}
    print("補上市除權息預告表...")
    schedule = [r for r in fetch_ex_dividend_schedule() if r["symbol"] in twse_symbols]
    with connect(config.db_path) as conn:
        if schedule:
            insert_ex_dividend_schedule(conn, schedule)
    print(f"  上市觀察清單裡有 {len(schedule)} 筆排定中的除權息")

    print("完成")


if __name__ == "__main__":
    main()
