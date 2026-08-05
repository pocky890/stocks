"""供dashboard在開啟時做一次「有新資料就抓，沒有就跳過」的檢查，
重用跟scripts/fetch_historical.py、scripts/fetch_market_data.py一樣的底層client/db函式，
只是包成一個安靜、不印進度的版本，適合在網頁載入時跑。"""
import time

from stocks.config import Config
from stocks.db import (
    connect,
    fetch_synced_market_dates,
    fetch_trading_dates,
    fetch_watchlist,
    init_db,
    insert_bars_daily,
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
from stocks.yfinance_client import fetch_symbol_bars


def _refresh_price_data(config: Config, symbols: list[str]) -> int:
    with connect(config.db_path) as conn:
        dates_before = set(fetch_trading_dates(conn))

    for symbol in symbols:
        bars = fetch_symbol_bars(symbol, period="5d")
        if bars:
            with connect(config.db_path) as conn:
                insert_bars_daily(conn, bars)
                upsert_symbol(conn, symbol, market="TWSE", is_watchlist=True)

    with connect(config.db_path) as conn:
        dates_after = set(fetch_trading_dates(conn))
    return len(dates_after - dates_before)


def _refresh_market_data(config: Config, symbols: set[str]) -> int:
    with connect(config.db_path) as conn:
        all_dates = fetch_trading_dates(conn)
        already_have = fetch_synced_market_dates(conn)
    todo_dates = [d for d in all_dates if d not in already_have]

    for date in todo_dates:
        flows = [r for r in fetch_institutional_flows_for_date(date) if r["symbol"] in symbols]
        time.sleep(config.batch_pacing_seconds)
        margins = [r for r in fetch_margin_balances_for_date(date) if r["symbol"] in symbols]
        time.sleep(config.batch_pacing_seconds)
        valuations = [r for r in fetch_valuations_for_date(date) if r["symbol"] in symbols]
        time.sleep(config.batch_pacing_seconds)

        with connect(config.db_path) as conn:
            if flows:
                insert_institutional_flows(conn, flows)
            if margins:
                insert_margin_balances(conn, margins)
            if valuations:
                insert_valuations(conn, valuations)
            mark_market_data_synced(conn, [date])

    schedule = [r for r in fetch_ex_dividend_schedule() if r["symbol"] in symbols]
    with connect(config.db_path) as conn:
        if schedule:
            insert_ex_dividend_schedule(conn, schedule)

    return len(todo_dates)


def check_and_update(config: Config) -> dict:
    """比對DB裡已有的交易日跟資料，缺什麼就抓什麼，沒有新資料就什麼都不做。"""
    init_db(config.db_path)

    with connect(config.db_path) as conn:
        watchlist = {row["code"] for row in fetch_watchlist(conn)}
    if not watchlist:
        return {"watchlist_empty": True, "new_price_days": 0, "new_market_days": 0}

    new_price_days = _refresh_price_data(config, sorted(watchlist))
    new_market_days = _refresh_market_data(config, watchlist)
    return {"watchlist_empty": False, "new_price_days": new_price_days, "new_market_days": new_market_days}
