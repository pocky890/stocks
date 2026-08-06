"""補三大法人買賣超、融資融券餘額、估值(PE/殖利率/PB)、除權息預告表——全部是免費公開資料，
跟Shioaji帳戶/金鑰狀態無關，現在就能跑。

上市(TWSE)：重用bars_daily已有的交易日清單，逐日呼叫backfill，可以補近1年歷史。
上櫃(TPEx)：免費API不支援指定日期查詢，每次呼叫只給「最新一天」，沒有--full這個選項可用，
   沒辦法一次補歷史，只能之後每天跑一次慢慢累積。
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stocks.config import load_config
from stocks.daily_update import _refresh_market_data_tpex
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
    upsert_symbol,
)
from stocks.twse_client import (
    fetch_ex_dividend_schedule,
    fetch_institutional_flows_for_date,
    fetch_margin_balances_for_date,
    fetch_valuations_for_date,
)


def main():
    parser = argparse.ArgumentParser(description="補三大法人/融資融券/估值/除權息資料")
    parser.add_argument("--full", action="store_true", help="上市股票強制重抓全部日期，不跳過已有的（上櫃沒有這個選項）")
    args = parser.parse_args()

    config = load_config()
    init_db(config.db_path)

    with connect(config.db_path) as conn:
        all_dates = fetch_trading_dates(conn)
        watchlist_rows = fetch_watchlist(conn)
        already_have = set() if args.full else fetch_synced_market_dates(conn)

    if not all_dates:
        print("bars_daily是空的，先跑 scripts/fetch_historical.py")
        return
    if not watchlist_rows:
        print("觀察清單是空的，先跑 scripts/fetch_historical.py")
        return

    twse_symbols = {r["code"] for r in watchlist_rows if r["market"] != "TPEx"}
    tpex_symbols = {r["code"] for r in watchlist_rows if r["market"] == "TPEx"}
    print(f"觀察清單: 上市 {sorted(twse_symbols)} / 上櫃 {sorted(tpex_symbols)}")

    dates = [d for d in all_dates if d not in already_have]
    print(f"[上市] 共 {len(all_dates)} 個交易日，已有 {len(already_have)} 天資料，還需要抓 {len(dates)} 天")

    if not dates:
        print("[上市] 資料已經是最新的，不需要抓")
    for i, date in enumerate(dates):
        flows = [r for r in fetch_institutional_flows_for_date(date) if r["symbol"] in twse_symbols]
        time.sleep(config.batch_pacing_seconds)
        margins = [r for r in fetch_margin_balances_for_date(date) if r["symbol"] in twse_symbols]
        time.sleep(config.batch_pacing_seconds)
        valuations = [r for r in fetch_valuations_for_date(date) if r["symbol"] in twse_symbols]
        time.sleep(config.batch_pacing_seconds)

        with connect(config.db_path) as conn:
            if flows:
                insert_institutional_flows(conn, flows)
            if margins:
                insert_margin_balances(conn, margins)
            if valuations:
                insert_valuations(conn, valuations)
                for row in valuations:
                    upsert_symbol(conn, row["symbol"], name=row["name"], market="TWSE", is_watchlist=True)
            mark_market_data_synced(conn, [date])

        if (i + 1) % 20 == 0 or i == len(dates) - 1:
            print(f"  進度 {i + 1}/{len(dates)} ({date})")

    print("[上市] 補除權息預告表...")
    schedule = [r for r in fetch_ex_dividend_schedule() if r["symbol"] in twse_symbols]
    with connect(config.db_path) as conn:
        if schedule:
            insert_ex_dividend_schedule(conn, schedule)
    print(f"  上市觀察清單裡有 {len(schedule)} 筆排定中的除權息")

    if tpex_symbols:
        print("[上櫃] 抓最新一天的三大法人/融資融券/估值/除權息...")
        synced = _refresh_market_data_tpex(config, tpex_symbols)
        print(f"  {'有新資料' if synced else '跟上次抓的是同一天，沒有新資料'}")

    print("完成")


if __name__ == "__main__":
    main()
