from datetime import datetime, timedelta

import backfill_missing_watchlist_data as backfill
from stocks import db
from stocks.models import Bar


def make_bar(symbol, ts, close=100.0):
    return Bar(symbol=symbol, ts=ts, open=close, high=close + 1, low=close - 1, close=close, volume=1000)


def test_backfill_prices_if_missing_skips_symbol_with_enough_history(tmp_path, monkeypatch):
    # 已經有足夠歷史的股票(例如原本就在這台電腦上、透過add_symbol_to_watchlist加的)不該
    # 重新打yfinance——這是這支腳本「只補真的缺資料的股票」的核心行為，呼叫到就代表壞了。
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    start = datetime(2020, 1, 1)
    with db.connect(db_path) as conn:
        db.insert_bars_daily(conn, [make_bar("2330", start + timedelta(days=i)) for i in range(250)])  # >=200筆相異日期

        def boom(code, period):
            raise AssertionError("不該呼叫yfinance，這支股票已經有足夠歷史")

        monkeypatch.setattr(backfill, "detect_market_and_fetch_bars", boom)
        added = backfill.backfill_prices_if_missing(conn, "2330")

        assert added == 0


def test_backfill_prices_if_missing_fetches_when_history_too_short(tmp_path, monkeypatch):
    # 2026-08-17實際案例：透過watchlist_shared.json同步進來的股票只有5~7筆bars_daily，
    # 這裡驗證這種情況下真的會觸發回補。
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        db.insert_bars_daily(conn, [make_bar("3141", datetime(2026, 8, i)) for i in range(1, 6)])  # 只有5筆

        fetched_bars = [make_bar("3141", datetime(2016, 1, i)) for i in range(1, 3)]
        monkeypatch.setattr(backfill, "detect_market_and_fetch_bars", lambda code, period: (fetched_bars, "TPEx"))

        added = backfill.backfill_prices_if_missing(conn, "3141")

        assert added == 2
        # 原本5筆(2026-08) + 新抓的2筆(2016-01，日期不重疊)，insert_bars_daily是INSERT OR
        # REPLACE，不會覆蓋掉原本已經有的那5筆。
        assert len(db.fetch_bars_daily(conn, "3141")) == 7


def test_backfill_prices_if_missing_returns_zero_when_fetch_finds_nothing(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        monkeypatch.setattr(backfill, "detect_market_and_fetch_bars", lambda code, period: ([], ""))
        added = backfill.backfill_prices_if_missing(conn, "9999")

    assert added == 0


def test_backfill_chip_data_if_missing_only_fetches_the_empty_ones(tmp_path, monkeypatch):
    # 三大法人/估值/月營收各自獨立檢查——已經有資料的那一項不該被覆蓋或重抓，
    # 只有真的0筆的項目才觸發FinMind呼叫。
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        db.insert_institutional_flows(
            conn, [{"symbol": "2330", "date": "2026-08-01", "foreign_net": 1, "trust_net": 1, "dealer_net": 1, "total_net": 3}]
        )

        def boom(*args):
            raise AssertionError("institutional_flows已經有資料，不該重新呼叫FinMind")

        monkeypatch.setattr(backfill.finmind_client, "fetch_institutional_flows_for_range", boom)
        monkeypatch.setattr(
            backfill.finmind_client,
            "fetch_valuations_for_range",
            lambda s, a, b: [{"symbol": s, "date": a, "pe_ratio": 10, "dividend_yield": 1, "pb_ratio": 1}],
        )
        monkeypatch.setattr(
            backfill.finmind_client,
            "fetch_monthly_revenue_for_range",
            lambda s, a, b: [{"symbol": s, "date": a, "revenue_year": 2026, "revenue_month": 7, "revenue": 100}],
        )

        result = backfill.backfill_chip_data_if_missing(conn, "2330", "2026-08-01", "2026-08-07")

        assert result == {"flows": 0, "valuations": 1, "revenue": 1}
        assert len(db.fetch_valuations(conn, "2330")) == 1
        assert len(db.fetch_monthly_revenue(conn, "2330")) == 1


def test_backfill_chip_data_if_missing_is_a_noop_when_everything_already_present(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        db.insert_institutional_flows(
            conn, [{"symbol": "2330", "date": "2026-08-01", "foreign_net": 1, "trust_net": 1, "dealer_net": 1, "total_net": 3}]
        )
        db.insert_valuations(conn, [{"symbol": "2330", "date": "2026-08-01", "pe_ratio": 10, "dividend_yield": 1, "pb_ratio": 1}])
        db.insert_monthly_revenue(
            conn, [{"symbol": "2330", "date": "2026-08-01", "revenue_year": 2026, "revenue_month": 7, "revenue": 100}]
        )

        def boom(*args):
            raise AssertionError("三項資料都已經存在，不該呼叫任何FinMind函式")

        monkeypatch.setattr(backfill.finmind_client, "fetch_institutional_flows_for_range", boom)
        monkeypatch.setattr(backfill.finmind_client, "fetch_valuations_for_range", boom)
        monkeypatch.setattr(backfill.finmind_client, "fetch_monthly_revenue_for_range", boom)

        result = backfill.backfill_chip_data_if_missing(conn, "2330", "2026-08-01", "2026-08-07")

    assert result == {"flows": 0, "valuations": 0, "revenue": 0}
