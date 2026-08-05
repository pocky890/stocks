"""補三大法人買賣超、融資融券餘額、估值(PE/殖利率/PB)、除權息預告表——全部是證交所免費公開資料，
跟Shioaji帳戶/金鑰狀態無關，現在就能跑。重用bars_daily已有的交易日清單，逐日呼叫，避免自己猜哪天休市。
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stocks.config import load_config
from stocks.db import (
    connect,
    fetch_synced_market_dates,
    fetch_trading_dates,
    fetch_watchlist,
    init_db,
    insert_ex_dividend_schedule,
    insert_institutional_flows,
    insert_margin_balances,
    insert_valuations,
    mark_market_data_synced,
)
from stocks.twse_client import (
    fetch_ex_dividend_schedule,
    fetch_institutional_flows_for_date,
    fetch_margin_balances_for_date,
    fetch_valuations_for_date,
)


def main():
    parser = argparse.ArgumentParser(description="補三大法人/融資融券/估值/除權息資料")
    parser.add_argument("--full", action="store_true", help="強制重抓全部日期，不跳過已有的")
    args = parser.parse_args()

    config = load_config()
    init_db(config.db_path)

    with connect(config.db_path) as conn:
        all_dates = fetch_trading_dates(conn)
        watchlist = {row["code"] for row in fetch_watchlist(conn)}
        already_have = set() if args.full else fetch_synced_market_dates(conn)

    if not all_dates:
        print("bars_daily是空的，先跑 scripts/fetch_historical.py")
        return
    if not watchlist:
        print("觀察清單是空的，先跑 scripts/fetch_historical.py")
        return

    dates = [d for d in all_dates if d not in already_have]
    print(f"觀察清單: {sorted(watchlist)}")
    print(f"共 {len(all_dates)} 個交易日，已有 {len(already_have)} 天資料，還需要抓 {len(dates)} 天")

    if not dates:
        print("資料已經是最新的，不需要抓")
    for i, date in enumerate(dates):
        flows = [r for r in fetch_institutional_flows_for_date(date) if r["symbol"] in watchlist]
        time.sleep(config.batch_pacing_seconds)
        margins = [r for r in fetch_margin_balances_for_date(date) if r["symbol"] in watchlist]
        time.sleep(config.batch_pacing_seconds)
        valuations = [r for r in fetch_valuations_for_date(date) if r["symbol"] in watchlist]
        time.sleep(config.batch_pacing_seconds)

        with connect(config.db_path) as conn:
            if flows:
                insert_institutional_flows(conn, flows)
            if margins:
                insert_margin_balances(conn, margins)
            if valuations:
                insert_valuations(conn, valuations)
            mark_market_data_synced(conn, [date])

        if (i + 1) % 20 == 0 or i == len(dates) - 1:
            print(f"  進度 {i + 1}/{len(dates)} ({date})")

    print("補除權息預告表...")
    schedule = [r for r in fetch_ex_dividend_schedule() if r["symbol"] in watchlist]
    with connect(config.db_path) as conn:
        if schedule:
            insert_ex_dividend_schedule(conn, schedule)
    print(f"  觀察清單裡有 {len(schedule)} 筆排定中的除權息")

    print("完成")


if __name__ == "__main__":
    main()
