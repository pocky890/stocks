"""供dashboard在開啟時做一次「有新資料就抓，沒有就跳過」的檢查，
重用跟scripts/fetch_historical.py、scripts/fetch_market_data.py一樣的底層client/db函式，
只是包成一個安靜、不印進度的版本，適合在網頁載入時跑。

上市(TWSE)跟上櫃(TPEx)的籌碼資料(三大法人/融資融券/估值)走不同邏輯：TWSE可以指定日期查詢，
用sync log追蹤還缺哪些日期backfill；TPEx的免費API不支援指定日期，每次呼叫只給「最新一天」，
沒辦法backfill歷史，只能每次都呼叫一次讓它慢慢累積。
"""
import time

from stocks import tpex_client, twse_client
from stocks.config import Config
from stocks.db import (
    add_to_watchlist,
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
from stocks.yfinance_client import detect_market_and_fetch_bars, fetch_symbol_bars


def _refresh_price_data(config: Config, symbols: list[str]) -> int:
    with connect(config.db_path) as conn:
        dates_before = set(fetch_trading_dates(conn))

    for symbol in symbols:
        bars, market = detect_market_and_fetch_bars(symbol, period="5d")
        if bars:
            with connect(config.db_path) as conn:
                insert_bars_daily(conn, bars)
                upsert_symbol(conn, symbol, market=market, is_watchlist=True)

    with connect(config.db_path) as conn:
        dates_after = set(fetch_trading_dates(conn))
    return len(dates_after - dates_before)


def _refresh_market_data_twse(config: Config, symbols: set[str]) -> int:
    if not symbols:
        return 0

    with connect(config.db_path) as conn:
        all_dates = fetch_trading_dates(conn)
        already_have = fetch_synced_market_dates(conn)
    todo_dates = [d for d in all_dates if d not in already_have]

    for date in todo_dates:
        flows = [r for r in twse_client.fetch_institutional_flows_for_date(date) if r["symbol"] in symbols]
        time.sleep(config.batch_pacing_seconds)
        margins = [r for r in twse_client.fetch_margin_balances_for_date(date) if r["symbol"] in symbols]
        time.sleep(config.batch_pacing_seconds)
        valuations = [r for r in twse_client.fetch_valuations_for_date(date) if r["symbol"] in symbols]
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

    schedule = [r for r in twse_client.fetch_ex_dividend_schedule() if r["symbol"] in symbols]
    with connect(config.db_path) as conn:
        if schedule:
            insert_ex_dividend_schedule(conn, schedule)

    return len(todo_dates)


def _fetched_dates_for_symbols(config: Config, symbols: set[str]) -> set[str]:
    placeholders = ",".join("?" * len(symbols))
    with connect(config.db_path) as conn:
        rows = conn.execute(
            f"SELECT DISTINCT date FROM institutional_flows WHERE symbol IN ({placeholders})",
            tuple(symbols),
        ).fetchall()
    return {r["date"] for r in rows}


def _refresh_market_data_tpex(config: Config, symbols: set[str]) -> bool:
    """TPEx沒有日期查詢，每次都直接抓「最新一天」，用前後比對日期集合來判斷是不是真的有新資料
    （不能只看「有沒有呼叫成功」，否則每次開app都會誤報「已更新」）。"""
    if not symbols:
        return False

    dates_before = _fetched_dates_for_symbols(config, symbols)

    flows = [r for r in tpex_client.fetch_institutional_flows_latest() if r["symbol"] in symbols]
    margins = [r for r in tpex_client.fetch_margin_balances_latest() if r["symbol"] in symbols]
    valuations = [r for r in tpex_client.fetch_valuations_latest() if r["symbol"] in symbols]
    schedule = [r for r in tpex_client.fetch_ex_dividend_schedule() if r["symbol"] in symbols]

    with connect(config.db_path) as conn:
        if flows:
            insert_institutional_flows(conn, flows)
        if margins:
            insert_margin_balances(conn, margins)
        if valuations:
            insert_valuations(conn, valuations)
            for row in valuations:
                upsert_symbol(conn, row["symbol"], name=row["name"], market="TPEx", is_watchlist=True)
        if schedule:
            insert_ex_dividend_schedule(conn, schedule)

    dates_after = _fetched_dates_for_symbols(config, symbols)
    return len(dates_after - dates_before) > 0


def add_symbol_to_watchlist(config: Config, code: str) -> dict:
    """新增一檔股票：自動判斷上市/上櫃，抓近1年股價，並只抓最新一天的三大法人/融資融券/估值
    （讓籌碼頁籤不是全空）。上市股票的舊日期籌碼資料因為sync log是以「日期」而非「日期+股票」為
    單位在追蹤，不會自動幫新股票補齊，需要的話手動跑一次 `python scripts/fetch_market_data.py --full`。
    上櫃股票本來就沒有歷史籌碼資料可以backfill（免費API限制），只能之後每天累積。"""
    code = code.strip()
    init_db(config.db_path)

    with connect(config.db_path) as conn:
        already_in = conn.execute(
            "SELECT 1 FROM symbols WHERE code = ? AND is_watchlist = 1", (code,)
        ).fetchone()
    if already_in:
        return {"ok": False, "message": f"{code} 已經在觀察清單裡了"}

    bars, market = detect_market_and_fetch_bars(code, period="1y")
    if not bars:
        return {"ok": False, "message": f"抓不到 {code} 的股價資料，確認代號是否正確"}

    with connect(config.db_path) as conn:
        insert_bars_daily(conn, bars)

    if market == "TPEx":
        flows = [r for r in tpex_client.fetch_institutional_flows_latest() if r["symbol"] == code]
        margins = [r for r in tpex_client.fetch_margin_balances_latest() if r["symbol"] == code]
        valuations = [r for r in tpex_client.fetch_valuations_latest() if r["symbol"] == code]
        chips_note = "上櫃股票的免費API不支援查歷史，之後每天會慢慢累積"
    else:
        latest_date = max(b.ts for b in bars).strftime("%Y-%m-%d")
        flows = [r for r in twse_client.fetch_institutional_flows_for_date(latest_date) if r["symbol"] == code]
        margins = [r for r in twse_client.fetch_margin_balances_for_date(latest_date) if r["symbol"] == code]
        valuations = [r for r in twse_client.fetch_valuations_for_date(latest_date) if r["symbol"] == code]
        chips_note = "完整歷史要另外跑 `python scripts/fetch_market_data.py --full` 補齊"

    name = valuations[0]["name"] if valuations else ""

    with connect(config.db_path) as conn:
        add_to_watchlist(conn, code, name=name, market=market)
        if flows:
            insert_institutional_flows(conn, flows)
        if margins:
            insert_margin_balances(conn, margins)
        if valuations:
            insert_valuations(conn, valuations)

    label = f"{code}（{name}）" if name else code
    market_label = "上市" if market == "TWSE" else "上櫃"
    return {
        "ok": True,
        "message": (
            f"已新增 {label}（{market_label}），近1年股價已抓好。"
            f"三大法人/融資融券/估值只先抓最新一天，{chips_note}"
        ),
    }


def check_and_update(config: Config) -> dict:
    """比對DB裡已有的交易日跟資料，缺什麼就抓什麼，沒有新資料就什麼都不做。"""
    init_db(config.db_path)

    with connect(config.db_path) as conn:
        rows = fetch_watchlist(conn)
    if not rows:
        return {"watchlist_empty": True, "new_price_days": 0, "new_market_days": 0, "otc_synced": False}

    watchlist = {r["code"] for r in rows}
    twse_symbols = {r["code"] for r in rows if r["market"] != "TPEx"}
    tpex_symbols = {r["code"] for r in rows if r["market"] == "TPEx"}

    new_price_days = _refresh_price_data(config, sorted(watchlist))
    new_market_days = _refresh_market_data_twse(config, twse_symbols)
    otc_synced = _refresh_market_data_tpex(config, tpex_symbols)

    return {
        "watchlist_empty": False,
        "new_price_days": new_price_days,
        "new_market_days": new_market_days,
        "otc_synced": otc_synced,
    }
