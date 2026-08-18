"""補三大法人買賣超、估值(PE/殖利率/PB)、月營收、除權息——全部是免費公開資料，
跟Shioaji帳戶/金鑰狀態無關，現在就能跑。

三大法人/估值/月營收/除權息的歷史回補全部改用FinMind(一支股票一次查詢就拿到整段歷史
範圍)，不分上市/上櫃——2026-08-17發現原本上市用TWSE官方API逐日查詢，10年歷史要打近
2500次、途中必然會遇到逾時；FinMind一支股票一次呼叫就拿完，上市上櫃都適用(上櫃官方API
本來就做不到逐日回補，只能靠FinMind)。每日例行更新(daily_update.py)維持用官方TWSE/TPEx
API追蹤「今天」的新資料，跟這裡的historical backfill是分開的兩條路，不受這裡改動影響——
但這裡跑完仍然要mark_market_data_synced，這樣daily_update.py的TWSE增量路徑才知道這段
歷史已經有人補過，不會每次開dashboard都想重新逐日補一次。

月營收(monthly_revenue)是2026-08-16新增的基本面濾網研究資料，不分上市/上櫃都用
`TaiwanStockMonthRevenue`同一個dataset，補法跟三大法人/估值一致；目前沒有任何策略
讀取這份資料。

除權息(ex_dividend_schedule)：2026-08-18改用FinMind的`TaiwanStockDividend`(完整歷史)
取代原本只有上市、且只列出「已公告但還沒發生」事件的TWSE官方預告表——原本的預告表一
過期就會把已發生的事件從清單上丟掉，這支腳本原本又只backfill上市(twse_symbols)，上櫃
股票完全沒有這份資料。改用FinMind後上市/上櫃都補、且能一次回補過去已發生的歷史事件，
不再受限於「目前排定中」。融資融券資料2026-08-16拿掉(dashboard圖表用不到，改成成交量
子圖)，不再抓取。
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
    insert_monthly_revenue,
    insert_valuations,
    mark_market_data_synced,
)
from stocks.finmind_client import (
    fetch_ex_dividend_schedule_for_range,
    fetch_institutional_flows_for_range,
    fetch_monthly_revenue_for_range,
    fetch_valuations_for_range,
)


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
    print(f"用FinMind補三大法人/估值/月營收/除權息歷史 {start_date}~{end_date}...")

    for symbol in symbols:
        flows = fetch_institutional_flows_for_range(symbol, start_date, end_date)
        valuations = fetch_valuations_for_range(symbol, start_date, end_date)
        revenue = fetch_monthly_revenue_for_range(symbol, start_date, end_date)
        ex_dividend = fetch_ex_dividend_schedule_for_range(symbol, start_date, end_date)
        with connect(config.db_path) as conn:
            if flows:
                insert_institutional_flows(conn, flows)
            if valuations:
                insert_valuations(conn, valuations)
            if revenue:
                insert_monthly_revenue(conn, revenue)
            if ex_dividend:
                insert_ex_dividend_schedule(conn, ex_dividend)
        print(f"  {symbol}: 籌碼{len(flows)}筆 / 估值{len(valuations)}筆 / 月營收{len(revenue)}筆 / 除權息{len(ex_dividend)}筆")

    with connect(config.db_path) as conn:
        mark_market_data_synced(conn, all_dates)

    print("完成")


if __name__ == "__main__":
    main()
