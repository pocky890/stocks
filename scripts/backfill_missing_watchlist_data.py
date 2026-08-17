"""定期背景掃描觀察清單，自動補齊資料不完整的股票——通常是透過watchlist_shared.json
跨機器同步進來的(而不是在這台電腦上用dashboard「新增股票」加的)，同步機制本身只同步
代號/名稱/市場/排序/群組，完全不會觸發`daily_update.add_symbol_to_watchlist()`那套
價格/三大法人/估值/月營收/產業代號回補。2026-08-17連續踩到兩次(28支股票monthly_
revenue是0筆、12支股票bars_daily只有5~7筆)才發現這個坑一直是靠人類記得手動跑
fetch_market_data.py/populate_industry_codes.py，沒有自動化。

只補真的缺資料的股票(用現有資料量判斷，不是每次全部重抓)，已經完整的股票不會重複打
API，所以排程頻繁執行也不會造成額外負擔——大多數時候每一支都已經補過，這次執行幾乎
是no-op。刻意獨立成排程腳本、不接在dashboard載入流程裡：回補歷史資料要花真實的網路
時間，接在dashboard裡會在剛同步進新股票那一次卡住UI，使用者2026-08-17明確要求背景
慢慢補、不要卡畫面。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stocks import finmind_client
from stocks.config import load_config
from stocks.db import (
    connect,
    fetch_bars_daily,
    fetch_institutional_flows,
    fetch_monthly_revenue,
    fetch_trading_dates,
    fetch_valuations,
    fetch_watchlist,
    init_db,
    insert_bars_daily,
    insert_institutional_flows,
    insert_monthly_revenue,
    insert_valuations,
)
from stocks.yfinance_client import detect_market_and_fetch_bars

MIN_BARS_DAILY = 200  # 低於這個數字視為「價格歷史沒真的回補過」——正常股票10年應該有~2400筆


def backfill_prices_if_missing(conn, code: str) -> int:
    """價格歷史筆數明顯不足才回補，回傳實際補了幾筆(0代表不需要補或抓不到資料)。"""
    if len(fetch_bars_daily(conn, code)) >= MIN_BARS_DAILY:
        return 0
    bars, _market = detect_market_and_fetch_bars(code, period="10y")
    if not bars:
        return 0
    insert_bars_daily(conn, bars)
    return len(bars)


def backfill_chip_data_if_missing(conn, code: str, start_date: str, end_date: str) -> dict:
    """三大法人/估值/月營收，各自獨立檢查是不是0筆，任一個失敗不影響其他兩個
    (跟add_symbol_to_watchlist()新增股票時同一套容錯慣例)。"""
    result = {"flows": 0, "valuations": 0, "revenue": 0}

    if not fetch_institutional_flows(conn, code):
        flows = finmind_client.fetch_institutional_flows_for_range(code, start_date, end_date)
        if flows:
            insert_institutional_flows(conn, flows)
            result["flows"] = len(flows)

    if not fetch_valuations(conn, code):
        valuations = finmind_client.fetch_valuations_for_range(code, start_date, end_date)
        if valuations:
            insert_valuations(conn, valuations)
            result["valuations"] = len(valuations)

    if not fetch_monthly_revenue(conn, code):
        revenue = finmind_client.fetch_monthly_revenue_for_range(code, start_date, end_date)
        if revenue:
            insert_monthly_revenue(conn, revenue)
            result["revenue"] = len(revenue)

    return result


def main():
    config = load_config()
    init_db(config.db_path)

    with connect(config.db_path) as conn:
        watchlist = fetch_watchlist(conn)

    any_backfilled = False
    for row in watchlist:
        code, name = row["code"], row["name"]
        with connect(config.db_path) as conn:
            added = backfill_prices_if_missing(conn, code)
        if added:
            print(f"{code} {name}: 價格歷史不足，已補{added}筆")
            any_backfilled = True

    with connect(config.db_path) as conn:
        all_dates = fetch_trading_dates(conn)
    if not all_dates:
        print("bars_daily是空的，先跑scripts/fetch_historical.py")
        return
    start_date, end_date = all_dates[0], all_dates[-1]

    for row in watchlist:
        code, name = row["code"], row["name"]
        with connect(config.db_path) as conn:
            result = backfill_chip_data_if_missing(conn, code, start_date, end_date)
        if any(result.values()):
            print(f"{code} {name}: 三大法人{result['flows']}筆/估值{result['valuations']}筆/月營收{result['revenue']}筆")
            any_backfilled = True

    # 產業代碼：重跑populate_industry_codes.py同一套邏輯(不只標記單一股票，也確保新
    # 同步進來的產業有全市場同業覆蓋供斷路器算產業寬度)——這支腳本本身就是idempotent，
    # 每次重跑成本是2次公司名錄抓取，不是逐檔API呼叫，天天背景跑也可以接受。
    from populate_industry_codes import main as populate_industry_codes_main

    populate_industry_codes_main()

    if not any_backfilled:
        print("觀察清單價格/三大法人/估值/月營收資料完整，沒有需要回補的股票")


if __name__ == "__main__":
    main()
