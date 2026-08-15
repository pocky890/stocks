from datetime import datetime, timedelta

import pandas as pd
import pytest

from run_live import build_daily_bars_with_today, todays_cash_dividend
from stocks import db
from stocks.models import Bar


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    with db.connect(db_path) as c:
        db.add_to_watchlist(c, "2330", name="台積電")
        yield c


def make_daily_bar(symbol, ts, close):
    return Bar(symbol=symbol, ts=ts, open=close, high=close + 1, low=close - 1, close=close, volume=1000)


def make_tick(symbol, ts, close):
    return Bar(symbol=symbol, ts=ts, open=close, high=close, low=close, close=close, volume=100)


def test_todays_cash_dividend_returns_amount_when_ex_date_is_today(conn):
    today = datetime.now().date()
    db.insert_ex_dividend_schedule(
        conn,
        [{"symbol": "2330", "ex_date": today.isoformat(), "cash_dividend": 10.0, "stock_dividend_ratio": 0, "detail": ""}],
    )
    assert todays_cash_dividend(conn, "2330", today) == 10.0


def test_todays_cash_dividend_returns_zero_when_no_schedule_matches_today(conn):
    today = datetime.now().date()
    other_date = (today + timedelta(days=30)).isoformat()
    db.insert_ex_dividend_schedule(
        conn,
        [{"symbol": "2330", "ex_date": other_date, "cash_dividend": 10.0, "stock_dividend_ratio": 0, "detail": ""}],
    )
    assert todays_cash_dividend(conn, "2330", today) == 0.0


def test_build_daily_bars_with_today_adds_back_dividend_for_partial_bar(conn):
    # 2026-08-15使用者發現的情境：昨天(不含今天股利)收在120，今天除息10元，即時報價
    # (Shioaji原始報價)收在100——如果直接拿100去跟根據120算出來的停損線比，會誤判成
    # 跌破，實際上加回股息後是110，並沒有真的跌破。
    yesterday = datetime.now() - timedelta(days=1)
    today = datetime.now().date()
    db.insert_bars_daily(conn, [make_daily_bar("2330", yesterday, 120.0)])
    db.insert_ex_dividend_schedule(
        conn,
        [{"symbol": "2330", "ex_date": today.isoformat(), "cash_dividend": 10.0, "stock_dividend_ratio": 0, "detail": ""}],
    )
    db.insert_bars_5min(conn, [make_tick("2330", datetime.now(), 100.0)])

    bars = build_daily_bars_with_today(conn, "2330")

    today_row = bars.loc[bars.index.date == today].iloc[0]
    assert today_row["close"] == 110.0, "今天的收盤要加回10元股利，回到除息前的價格基準"
    assert today_row["open"] == 110.0
    assert today_row["high"] == 110.0
    assert today_row["low"] == 110.0


def test_build_daily_bars_with_today_adds_back_dividend_for_official_bar_already_present(conn):
    # 如果run_batch.py已經跑過、bars_daily裡已經有今天的正式K棒(一樣是原始報價，不是
    # yfinance還原後的資料)，一樣要補回股利，不是只有partial K棒那個分支需要處理。
    today = datetime.now()
    db.insert_bars_daily(conn, [make_daily_bar("2330", today, 100.0)])
    db.insert_ex_dividend_schedule(
        conn,
        [
            {
                "symbol": "2330",
                "ex_date": today.date().isoformat(),
                "cash_dividend": 10.0,
                "stock_dividend_ratio": 0,
                "detail": "",
            }
        ],
    )

    bars = build_daily_bars_with_today(conn, "2330")

    today_row = bars.loc[bars.index.date == today.date()].iloc[0]
    assert today_row["close"] == 110.0


def test_build_daily_bars_with_today_leaves_prices_unchanged_without_dividend_today(conn):
    yesterday = datetime.now() - timedelta(days=1)
    db.insert_bars_daily(conn, [make_daily_bar("2330", yesterday, 120.0)])
    db.insert_bars_5min(conn, [make_tick("2330", datetime.now(), 100.0)])

    bars = build_daily_bars_with_today(conn, "2330")

    today_row = bars.loc[bars.index.date == datetime.now().date()].iloc[0]
    assert today_row["close"] == 100.0, "沒有除息事件就不該動任何價格"
