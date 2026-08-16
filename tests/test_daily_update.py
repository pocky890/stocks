from datetime import datetime, timedelta

import requests

from stocks import daily_update
from stocks.config import Config
from stocks.db import add_to_watchlist, connect, fetch_signal_events, get_disabled_strategies, init_db, insert_signal_events
from stocks.models import Bar, Direction, SignalEvent, Tier
from stocks.notifier import NOTIFIABLE_STRATEGIES


def make_config(db_path: str) -> Config:
    return Config(
        shioaji_api_key="",
        shioaji_secret_key="",
        telegram_bot_token="",
        telegram_chat_id="",
        market_open="09:00",
        market_close="13:30",
        bar_interval_minutes=5,
        batch_pacing_seconds=0,
        strategy_params={},
        db_path=db_path,
    )


def test_check_and_update_survives_one_source_failing(tmp_path, monkeypatch):
    """A network blip hitting one data source (TPEx's flaky SSL cert is a real example)
    must not crash the whole dashboard load -- the other sources should still update
    and the failure should be reported, not swallowed silently or raised."""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    with connect(db_path) as conn:
        add_to_watchlist(conn, "2330", market="TWSE")

    config = make_config(db_path)

    monkeypatch.setattr(daily_update, "_refresh_price_data", lambda cfg, symbols: 1)
    monkeypatch.setattr(daily_update, "_refresh_market_data_twse", lambda cfg, symbols: 2)
    monkeypatch.setattr(daily_update, "_refresh_monthly_revenue", lambda cfg, symbols: False)

    def raise_ssl_error(cfg, symbols):
        raise requests.exceptions.SSLError("certificate verify failed")

    monkeypatch.setattr(daily_update, "_refresh_market_data_tpex", raise_ssl_error)

    result = daily_update.check_and_update(config)

    assert result["new_price_days"] == 1, "price refresh succeeded and should still be reported"
    assert result["new_market_days"] == 2, "TWSE refresh succeeded and should still be reported"
    assert result["otc_synced"] is False, "the failed source falls back to its default, not a crash"
    assert len(result["errors"]) == 1
    assert "上櫃" in result["errors"][0]


def test_check_and_update_reports_no_errors_when_everything_succeeds(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    with connect(db_path) as conn:
        add_to_watchlist(conn, "2330", market="TWSE")

    config = make_config(db_path)
    monkeypatch.setattr(daily_update, "_refresh_price_data", lambda cfg, symbols: 0)
    monkeypatch.setattr(daily_update, "_refresh_market_data_twse", lambda cfg, symbols: 0)
    monkeypatch.setattr(daily_update, "_refresh_market_data_tpex", lambda cfg, symbols: False)
    monkeypatch.setattr(daily_update, "_refresh_monthly_revenue", lambda cfg, symbols: False)

    result = daily_update.check_and_update(config)

    assert result["errors"] == []


def test_check_and_update_prunes_signal_events_older_than_retention(tmp_path, monkeypatch):
    # 2026-08-14使用者要求「訊號歷史紀錄」只留3個月——每次check_and_update跑(一天最多
    # 兩次)順便清掉超過保留期限的舊紀錄，不用另外排一個獨立的清理排程
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    with connect(db_path) as conn:
        add_to_watchlist(conn, "2330", market="TWSE")
        old_event = SignalEvent(
            symbol="2330", strategy="ma_crossover", direction=Direction.BUY, price=600.0,
            ts=datetime.now() - timedelta(days=91), detail="old", tier=Tier.REALTIME,
        )
        insert_signal_events(conn, [old_event])

    config = make_config(db_path)
    monkeypatch.setattr(daily_update, "_refresh_price_data", lambda cfg, symbols: 0)
    monkeypatch.setattr(daily_update, "_refresh_market_data_twse", lambda cfg, symbols: 0)
    monkeypatch.setattr(daily_update, "_refresh_market_data_tpex", lambda cfg, symbols: False)
    monkeypatch.setattr(daily_update, "_refresh_monthly_revenue", lambda cfg, symbols: False)

    daily_update.check_and_update(config)

    with connect(db_path) as conn:
        rows = fetch_signal_events(conn)
    assert rows == []


def test_add_symbol_falls_back_to_earlier_date_when_todays_valuation_report_not_out_yet(tmp_path, monkeypatch):
    """TWSE的每日估值報告有公布時間差：股價K棒可能已經有today的資料，但today的估值報告
    還沒公布，這時查today的valuations會是空的，名字不該就此留空(觀察清單顯示「—」)——
    應該退回去找最近幾天有資料的那天，反正公司名字不會變。"""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    config = make_config(db_path)

    today = datetime.now()
    bar = Bar(symbol="2408", ts=today, open=10, high=11, low=9, close=10, volume=100)
    monkeypatch.setattr(daily_update, "detect_market_and_fetch_bars", lambda code, period: ([bar], "TWSE"))

    def fake_valuations(date_str):
        if date_str == today.strftime("%Y-%m-%d"):
            return []  # 當天的估值報告還沒出來
        return [{"symbol": "2408", "name": "南亞科", "pe_ratio": 10, "dividend_yield": 1, "pb_ratio": 1}]

    monkeypatch.setattr(daily_update.twse_client, "fetch_valuations_for_date", fake_valuations)
    monkeypatch.setattr(daily_update.finmind_client, "fetch_institutional_flows_for_range", lambda s, a, b: [])
    monkeypatch.setattr(daily_update.finmind_client, "fetch_valuations_for_range", lambda s, a, b: [])
    monkeypatch.setattr(daily_update.finmind_client, "fetch_monthly_revenue_for_range", lambda s, a, b: [])
    monkeypatch.setattr(daily_update.twse_client, "fetch_company_directory", lambda: [])
    monkeypatch.setattr(daily_update.tpex_client, "fetch_company_directory", lambda: [])

    result = daily_update.add_symbol_to_watchlist(config, "2408")

    assert result["ok"] is True
    assert "南亞科" in result["message"]
    with connect(db_path) as conn:
        row = conn.execute("SELECT name FROM symbols WHERE code = '2408'").fetchone()
    assert row["name"] == "南亞科"


def test_add_symbol_immediately_computes_disabled_strategies(tmp_path, monkeypatch):
    """新增股票不用等下個月的排程，當下就該幫它跑一次排除評估——資料通常還很少(只有
    1根K棒)，NOTIFIABLE_STRATEGIES都湊不出足夠的交易次數，2026-08-08使用者確認樣本
    不足要保守排除(不是給寬限期預設保留)，理當全部排除，新股票會先整組安靜，等資料
    累積夠了下次重跑才會開始有判斷結果。"""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    config = make_config(db_path)

    today = datetime.now()
    bar = Bar(symbol="2408", ts=today, open=10, high=11, low=9, close=10, volume=100)
    monkeypatch.setattr(daily_update, "detect_market_and_fetch_bars", lambda code, period: ([bar], "TWSE"))
    monkeypatch.setattr(daily_update.twse_client, "fetch_valuations_for_date", lambda d: [])
    monkeypatch.setattr(daily_update.finmind_client, "fetch_institutional_flows_for_range", lambda s, a, b: [])
    monkeypatch.setattr(daily_update.finmind_client, "fetch_valuations_for_range", lambda s, a, b: [])
    monkeypatch.setattr(daily_update.finmind_client, "fetch_monthly_revenue_for_range", lambda s, a, b: [])
    monkeypatch.setattr(daily_update.finmind_client, "fetch_stock_name", lambda code: "")
    monkeypatch.setattr(daily_update.twse_client, "fetch_company_directory", lambda: [])
    monkeypatch.setattr(daily_update.tpex_client, "fetch_company_directory", lambda: [])

    daily_update.add_symbol_to_watchlist(config, "2408")

    with connect(db_path) as conn:
        assert set(get_disabled_strategies(conn, "2408")) == NOTIFIABLE_STRATEGIES


def test_add_symbol_tpex_backfills_three_years_via_finmind(tmp_path, monkeypatch):
    """上櫃股票新增時，三大法人買賣超改用FinMind一次補到跟股價一樣的3年範圍(不是舊版
    tpex_client.fetch_institutional_flows_latest()那種只有最新一天)——驗證傳給
    finmind_client的日期範圍是bars的最早跟最晚那天，不是隨便一個範圍。"""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    config = make_config(db_path)

    bars = [
        Bar(symbol="8299", ts=datetime(2023, 8, 7), open=10, high=11, low=9, close=10, volume=100),
        Bar(symbol="8299", ts=datetime(2026, 8, 7), open=12, high=13, low=11, close=12, volume=200),
    ]
    monkeypatch.setattr(daily_update, "detect_market_and_fetch_bars", lambda code, period: (bars, "TPEx"))
    monkeypatch.setattr(daily_update.tpex_client, "fetch_valuations_latest", lambda: [])
    monkeypatch.setattr(daily_update.finmind_client, "fetch_stock_name", lambda code: "")
    monkeypatch.setattr(daily_update.twse_client, "fetch_company_directory", lambda: [])
    monkeypatch.setattr(daily_update.tpex_client, "fetch_company_directory", lambda: [])

    captured = {}

    def fake_flows(symbol, start_date, end_date):
        captured["args"] = (symbol, start_date, end_date)
        return [{"symbol": symbol, "date": start_date, "foreign_net": 100, "trust_net": 50, "dealer_net": 0, "total_net": 150}]

    monkeypatch.setattr(daily_update.finmind_client, "fetch_institutional_flows_for_range", fake_flows)
    monkeypatch.setattr(daily_update.finmind_client, "fetch_valuations_for_range", lambda s, a, b: [])
    monkeypatch.setattr(daily_update.finmind_client, "fetch_monthly_revenue_for_range", lambda s, a, b: [])

    result = daily_update.add_symbol_to_watchlist(config, "8299")

    assert result["ok"] is True
    assert captured["args"] == ("8299", "2023-08-07", "2026-08-07")
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM institutional_flows WHERE symbol = '8299'").fetchone()
    assert row["foreign_net"] == 100


def test_add_symbol_tpex_falls_back_to_finmind_when_valuation_ssl_fails(tmp_path, monkeypatch):
    """2026-08-13修正：新增上櫃股票時查估值(順便拿名稱)那段沒有try/except保護，
    www.tpex.org.tw常有的SSL憑證問題會讓整個新增股票crash——先改成失敗時退回name=""，
    2026-08-16再加一層：TPEx官方查詢失敗時改問FinMind的TaiwanStockInfo當備援，這個
    dataset不受tpex.org.tw的SSL問題影響，通常查得到名稱，不用再等下次每日更新才補上。"""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    config = make_config(db_path)

    bar = Bar(symbol="3595", ts=datetime(2026, 8, 7), open=10, high=11, low=9, close=10, volume=100)
    monkeypatch.setattr(daily_update, "detect_market_and_fetch_bars", lambda code, period: ([bar], "TPEx"))
    monkeypatch.setattr(daily_update.finmind_client, "fetch_institutional_flows_for_range", lambda s, a, b: [])
    monkeypatch.setattr(daily_update.finmind_client, "fetch_valuations_for_range", lambda s, a, b: [])
    monkeypatch.setattr(daily_update.finmind_client, "fetch_monthly_revenue_for_range", lambda s, a, b: [])
    monkeypatch.setattr(daily_update.twse_client, "fetch_company_directory", lambda: [])
    monkeypatch.setattr(daily_update.tpex_client, "fetch_company_directory", lambda: [])

    def raise_ssl_error():
        raise requests.exceptions.SSLError("Missing Subject Key Identifier")

    monkeypatch.setattr(daily_update.tpex_client, "fetch_valuations_latest", raise_ssl_error)
    monkeypatch.setattr(daily_update.finmind_client, "fetch_stock_name", lambda code: "山太士")

    result = daily_update.add_symbol_to_watchlist(config, "3595")

    assert result["ok"] is True
    with connect(db_path) as conn:
        row = conn.execute("SELECT code, name FROM symbols WHERE code = '3595'").fetchone()
    assert row["code"] == "3595"
    assert row["name"] == "山太士"


def test_add_symbol_tpex_survives_when_finmind_name_fallback_also_fails(tmp_path, monkeypatch):
    """TPEx官方查詢跟FinMind備援都失敗時，還是要退回name=""、股票照樣加得進觀察清單，
    不是讓新增股票整個crash——下次每日更新查得到任一來源時會補上。"""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    config = make_config(db_path)

    bar = Bar(symbol="8299", ts=datetime(2026, 8, 7), open=10, high=11, low=9, close=10, volume=100)
    monkeypatch.setattr(daily_update, "detect_market_and_fetch_bars", lambda code, period: ([bar], "TPEx"))
    monkeypatch.setattr(daily_update.finmind_client, "fetch_institutional_flows_for_range", lambda s, a, b: [])
    monkeypatch.setattr(daily_update.finmind_client, "fetch_valuations_for_range", lambda s, a, b: [])
    monkeypatch.setattr(daily_update.finmind_client, "fetch_monthly_revenue_for_range", lambda s, a, b: [])
    monkeypatch.setattr(daily_update.twse_client, "fetch_company_directory", lambda: [])
    monkeypatch.setattr(daily_update.tpex_client, "fetch_company_directory", lambda: [])

    def raise_ssl_error():
        raise requests.exceptions.SSLError("Missing Subject Key Identifier")

    def raise_ssl_error_for_code(code):
        raise requests.exceptions.SSLError("Missing Subject Key Identifier")

    monkeypatch.setattr(daily_update.tpex_client, "fetch_valuations_latest", raise_ssl_error)
    monkeypatch.setattr(daily_update.finmind_client, "fetch_stock_name", raise_ssl_error_for_code)

    result = daily_update.add_symbol_to_watchlist(config, "8299")

    assert result["ok"] is True
    with connect(db_path) as conn:
        row = conn.execute("SELECT code, name FROM symbols WHERE code = '8299'").fetchone()
    assert row["code"] == "8299"
    assert row["name"] == ""


def test_add_symbol_twse_also_backfills_three_years_via_finmind(tmp_path, monkeypatch):
    """2026-08-08修正：上市股票新增時，三大法人買賣超原本只抓最新一天(sync log用「日期」
    為單位追蹤，新股票不會自動觸發回頭補值)，改成跟上櫃一樣直接用FinMind一次補到跟股價
    一樣的3年範圍，不用再手動跑fetch_market_data.py --full。"""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    config = make_config(db_path)

    bars = [
        Bar(symbol="2408", ts=datetime(2023, 8, 7), open=10, high=11, low=9, close=10, volume=100),
        Bar(symbol="2408", ts=datetime(2026, 8, 7), open=12, high=13, low=11, close=12, volume=200),
    ]
    monkeypatch.setattr(daily_update, "detect_market_and_fetch_bars", lambda code, period: (bars, "TWSE"))
    monkeypatch.setattr(
        daily_update.twse_client,
        "fetch_valuations_for_date",
        lambda d: [{"symbol": "2408", "name": "南亞科", "date": d, "pe_ratio": 10, "dividend_yield": 1, "pb_ratio": 1}],
    )
    monkeypatch.setattr(daily_update.twse_client, "fetch_company_directory", lambda: [])
    monkeypatch.setattr(daily_update.tpex_client, "fetch_company_directory", lambda: [])

    captured = {}

    def fake_flows(symbol, start_date, end_date):
        captured["args"] = (symbol, start_date, end_date)
        return [{"symbol": symbol, "date": start_date, "foreign_net": 100, "trust_net": 50, "dealer_net": 0, "total_net": 150}]

    monkeypatch.setattr(daily_update.finmind_client, "fetch_institutional_flows_for_range", fake_flows)
    monkeypatch.setattr(daily_update.finmind_client, "fetch_valuations_for_range", lambda s, a, b: [])
    monkeypatch.setattr(daily_update.finmind_client, "fetch_monthly_revenue_for_range", lambda s, a, b: [])

    result = daily_update.add_symbol_to_watchlist(config, "2408")

    assert result["ok"] is True
    assert captured["args"] == ("2408", "2023-08-07", "2026-08-07")
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM institutional_flows WHERE symbol = '2408'").fetchone()
    assert row["foreign_net"] == 100


def test_add_symbol_twse_backfills_valuation_history_via_finmind(tmp_path, monkeypatch):
    """2026-08-16修正：新增股票時估值(PE/殖利率/PB)之前只抓最新一天，其實
    finmind_client.fetch_valuations_for_range()早就存在(既有股票的每日增量回補一直
    在用)，只是新增股票這裡沒接上——比照三大法人，補到跟股價一樣的完整範圍。"""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    config = make_config(db_path)

    bars = [
        Bar(symbol="2408", ts=datetime(2023, 8, 7), open=10, high=11, low=9, close=10, volume=100),
        Bar(symbol="2408", ts=datetime(2026, 8, 7), open=12, high=13, low=11, close=12, volume=200),
    ]
    monkeypatch.setattr(daily_update, "detect_market_and_fetch_bars", lambda code, period: (bars, "TWSE"))
    monkeypatch.setattr(
        daily_update.twse_client,
        "fetch_valuations_for_date",
        lambda d: [{"symbol": "2408", "name": "南亞科", "date": d, "pe_ratio": 10, "dividend_yield": 1, "pb_ratio": 1}],
    )
    monkeypatch.setattr(daily_update.twse_client, "fetch_company_directory", lambda: [])
    monkeypatch.setattr(daily_update.tpex_client, "fetch_company_directory", lambda: [])
    monkeypatch.setattr(daily_update.finmind_client, "fetch_institutional_flows_for_range", lambda s, a, b: [])

    captured = {}

    def fake_valuation_history(symbol, start_date, end_date):
        captured["args"] = (symbol, start_date, end_date)
        return [{"symbol": symbol, "date": start_date, "pe_ratio": 8.0, "dividend_yield": 3.0, "pb_ratio": 1.5}]

    monkeypatch.setattr(daily_update.finmind_client, "fetch_valuations_for_range", fake_valuation_history)
    monkeypatch.setattr(daily_update.finmind_client, "fetch_monthly_revenue_for_range", lambda s, a, b: [])

    result = daily_update.add_symbol_to_watchlist(config, "2408")

    assert result["ok"] is True
    assert captured["args"] == ("2408", "2023-08-07", "2026-08-07")
    with connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM valuations WHERE symbol = '2408' ORDER BY date").fetchall()
    # 歷史回補那筆(2023-08-07)跟官方當天快照(2026-08-07)都要進去，且當天那筆維持官方
    # 來源的值(pe_ratio=10)，不會被FinMind同一天的資料覆蓋掉——這裡兩者日期不同，
    # 不會互相覆蓋，各自都在。
    assert {r["date"] for r in rows} == {"2023-08-07", "2026-08-07"}
    history_row = next(r for r in rows if r["date"] == "2023-08-07")
    assert history_row["pe_ratio"] == 8.0
    today_row = next(r for r in rows if r["date"] == "2026-08-07")
    assert today_row["pe_ratio"] == 10


def test_add_symbol_valuation_history_failure_does_not_block_flows(tmp_path, monkeypatch):
    """估值歷史回補失敗不該讓三大法人回補或整個新增流程失敗——跟三大法人失敗時的
    容錯是對稱的兩個獨立try/except。"""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    config = make_config(db_path)

    bar = Bar(symbol="2408", ts=datetime(2026, 8, 7), open=10, high=11, low=9, close=10, volume=100)
    monkeypatch.setattr(daily_update, "detect_market_and_fetch_bars", lambda code, period: ([bar], "TWSE"))
    monkeypatch.setattr(daily_update.twse_client, "fetch_valuations_for_date", lambda d: [])
    monkeypatch.setattr(daily_update.finmind_client, "fetch_stock_name", lambda code: "")
    monkeypatch.setattr(daily_update.twse_client, "fetch_company_directory", lambda: [])
    monkeypatch.setattr(daily_update.tpex_client, "fetch_company_directory", lambda: [])
    monkeypatch.setattr(
        daily_update.finmind_client,
        "fetch_institutional_flows_for_range",
        lambda s, a, b: [{"symbol": s, "date": b, "foreign_net": 100, "trust_net": 50, "dealer_net": 0, "total_net": 150}],
    )

    def raise_error(symbol, start_date, end_date):
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(daily_update.finmind_client, "fetch_valuations_for_range", raise_error)
    monkeypatch.setattr(daily_update.finmind_client, "fetch_monthly_revenue_for_range", lambda s, a, b: [])

    result = daily_update.add_symbol_to_watchlist(config, "2408")

    assert result["ok"] is True
    with connect(db_path) as conn:
        flow_row = conn.execute("SELECT * FROM institutional_flows WHERE symbol = '2408'").fetchone()
        valuation_row = conn.execute("SELECT * FROM valuations WHERE symbol = '2408'").fetchone()
    assert flow_row["foreign_net"] == 100
    assert valuation_row is None


def test_add_symbol_twse_backfills_monthly_revenue_via_finmind(tmp_path, monkeypatch):
    """2026-08-16新增：新股票加入時月營收也比照三大法人/估值，用FinMind一次補到跟股價
    一樣的完整範圍——基本面濾網研究用，目前沒有任何策略讀取這份資料。"""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    config = make_config(db_path)

    bars = [
        Bar(symbol="2408", ts=datetime(2023, 8, 7), open=10, high=11, low=9, close=10, volume=100),
        Bar(symbol="2408", ts=datetime(2026, 8, 7), open=12, high=13, low=11, close=12, volume=200),
    ]
    monkeypatch.setattr(daily_update, "detect_market_and_fetch_bars", lambda code, period: (bars, "TWSE"))
    monkeypatch.setattr(
        daily_update.twse_client,
        "fetch_valuations_for_date",
        lambda d: [{"symbol": "2408", "name": "南亞科", "date": d, "pe_ratio": 10, "dividend_yield": 1, "pb_ratio": 1}],
    )
    monkeypatch.setattr(daily_update.twse_client, "fetch_company_directory", lambda: [])
    monkeypatch.setattr(daily_update.tpex_client, "fetch_company_directory", lambda: [])
    monkeypatch.setattr(daily_update.finmind_client, "fetch_institutional_flows_for_range", lambda s, a, b: [])
    monkeypatch.setattr(daily_update.finmind_client, "fetch_valuations_for_range", lambda s, a, b: [])

    captured = {}

    def fake_revenue_history(symbol, start_date, end_date):
        captured["args"] = (symbol, start_date, end_date)
        return [{"symbol": symbol, "date": start_date, "revenue_year": 2023, "revenue_month": 7, "revenue": 500}]

    monkeypatch.setattr(daily_update.finmind_client, "fetch_monthly_revenue_for_range", fake_revenue_history)

    result = daily_update.add_symbol_to_watchlist(config, "2408")

    assert result["ok"] is True
    assert captured["args"] == ("2408", "2023-08-07", "2026-08-07")
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM monthly_revenue WHERE symbol = '2408'").fetchone()
    assert row["revenue"] == 500
    assert row["revenue_year"] == 2023
    assert row["revenue_month"] == 7


def test_add_symbol_monthly_revenue_failure_does_not_block_flows(tmp_path, monkeypatch):
    """月營收歷史回補失敗不該讓三大法人/估值回補或整個新增流程失敗——三個FinMind呼叫
    各自獨立try/except。"""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    config = make_config(db_path)

    bar = Bar(symbol="2408", ts=datetime(2026, 8, 7), open=10, high=11, low=9, close=10, volume=100)
    monkeypatch.setattr(daily_update, "detect_market_and_fetch_bars", lambda code, period: ([bar], "TWSE"))
    monkeypatch.setattr(daily_update.twse_client, "fetch_valuations_for_date", lambda d: [])
    monkeypatch.setattr(daily_update.finmind_client, "fetch_stock_name", lambda code: "")
    monkeypatch.setattr(daily_update.twse_client, "fetch_company_directory", lambda: [])
    monkeypatch.setattr(daily_update.tpex_client, "fetch_company_directory", lambda: [])
    monkeypatch.setattr(
        daily_update.finmind_client,
        "fetch_institutional_flows_for_range",
        lambda s, a, b: [{"symbol": s, "date": b, "foreign_net": 100, "trust_net": 50, "dealer_net": 0, "total_net": 150}],
    )
    monkeypatch.setattr(daily_update.finmind_client, "fetch_valuations_for_range", lambda s, a, b: [])

    def raise_error(symbol, start_date, end_date):
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(daily_update.finmind_client, "fetch_monthly_revenue_for_range", raise_error)

    result = daily_update.add_symbol_to_watchlist(config, "2408")

    assert result["ok"] is True
    with connect(db_path) as conn:
        flow_row = conn.execute("SELECT * FROM institutional_flows WHERE symbol = '2408'").fetchone()
        revenue_row = conn.execute("SELECT * FROM monthly_revenue WHERE symbol = '2408'").fetchone()
    assert flow_row["foreign_net"] == 100
    assert revenue_row is None


def test_refresh_monthly_revenue_queries_from_day_after_last_synced_date(tmp_path, monkeypatch):
    """既有股票的月營收增量更新：查詢範圍是「這支股票monthly_revenue最後一筆日期+1」~
    「今天」，跟三大法人/估值的TPEx增量路徑同一套_fetch_range_per_symbol邏輯，只是
    不分市場(月營收兩個市場共用同一個FinMind dataset)。"""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    config = make_config(db_path)

    with connect(db_path) as conn:
        add_to_watchlist(conn, "2330", market="TWSE")
        conn.execute(
            "INSERT INTO monthly_revenue (symbol, date, revenue_year, revenue_month, revenue) "
            "VALUES ('2330', '2026-08-01', 2026, 7, 1000)"
        )

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls):
            return datetime(2026, 8, 7)

    monkeypatch.setattr(daily_update, "datetime", FrozenDateTime)

    captured = {}

    def fake_revenue(symbol, start_date, end_date):
        captured["args"] = (symbol, start_date, end_date)
        return []

    monkeypatch.setattr(daily_update.finmind_client, "fetch_monthly_revenue_for_range", fake_revenue)

    daily_update._refresh_monthly_revenue(config, {"2330"})

    assert captured["args"] == ("2330", "2026-08-02", "2026-08-07")


def test_refresh_monthly_revenue_failure_returns_false_without_crashing(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    config = make_config(db_path)

    with connect(db_path) as conn:
        add_to_watchlist(conn, "2330", market="TWSE")

    def raise_error(symbol, start_date, end_date):
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(daily_update.finmind_client, "fetch_monthly_revenue_for_range", raise_error)

    result = daily_update._refresh_monthly_revenue(config, {"2330"})

    assert result is False


def test_add_symbol_twse_falls_back_to_latest_day_when_finmind_fails(tmp_path, monkeypatch):
    """FinMind額度用盡/連線失敗不該讓新增股票整個失敗——退回沒有三大法人歷史，之後每天
    累積即可，跟原本「上市只有最新一天」的舊行為效果一樣，只是這次是意外情況而非設計。"""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    config = make_config(db_path)

    bar = Bar(symbol="2408", ts=datetime(2026, 8, 7), open=10, high=11, low=9, close=10, volume=100)
    monkeypatch.setattr(daily_update, "detect_market_and_fetch_bars", lambda code, period: ([bar], "TWSE"))
    monkeypatch.setattr(
        daily_update.twse_client,
        "fetch_valuations_for_date",
        lambda d: [{"symbol": "2408", "name": "南亞科", "date": d, "pe_ratio": 10, "dividend_yield": 1, "pb_ratio": 1}],
    )
    monkeypatch.setattr(daily_update.twse_client, "fetch_company_directory", lambda: [])
    monkeypatch.setattr(daily_update.tpex_client, "fetch_company_directory", lambda: [])

    def raise_error(symbol, start_date, end_date):
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(daily_update.finmind_client, "fetch_institutional_flows_for_range", raise_error)
    monkeypatch.setattr(daily_update.finmind_client, "fetch_valuations_for_range", lambda s, a, b: [])
    monkeypatch.setattr(daily_update.finmind_client, "fetch_monthly_revenue_for_range", lambda s, a, b: [])

    result = daily_update.add_symbol_to_watchlist(config, "2408")

    assert result["ok"] is True
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM institutional_flows WHERE symbol = '2408'").fetchone()
    assert row is None


def test_resolve_symbol_input_passes_through_plain_numeric_code_unchanged():
    # 純代號不含中文字，不該去查公司名錄——不mock directory函式，如果誤觸發呼叫會直接
    # 打真實API連線失敗，測試自然會爆炸(這正是要驗證的行為：完全不呼叫)
    assert daily_update._resolve_symbol_input("2330") == ("2330", "")


def test_resolve_symbol_input_resolves_chinese_name_via_exact_match(monkeypatch):
    monkeypatch.setattr(
        daily_update.twse_client, "fetch_company_directory", lambda: [{"symbol": "2330", "name": "台積電"}]
    )
    monkeypatch.setattr(daily_update.tpex_client, "fetch_company_directory", lambda: [{"symbol": "8299", "name": "群聯"}])

    assert daily_update._resolve_symbol_input("台積電") == ("2330", "")


def test_resolve_symbol_input_resolves_via_unique_partial_match(monkeypatch):
    # 使用者打的名稱不用完全一致，只要在全部候選裡唯一符合部分比對就採用
    directory = [{"symbol": "2330", "name": "台灣積體電路"}]
    monkeypatch.setattr(daily_update.twse_client, "fetch_company_directory", lambda: directory)
    monkeypatch.setattr(daily_update.tpex_client, "fetch_company_directory", lambda: [])

    assert daily_update._resolve_symbol_input("台灣積體") == ("2330", "")


def test_resolve_symbol_input_reports_ambiguous_partial_matches(monkeypatch):
    directory = [{"symbol": "2330", "name": "台積電"}, {"symbol": "6488", "name": "台積寶"}]
    monkeypatch.setattr(daily_update.twse_client, "fetch_company_directory", lambda: directory)
    monkeypatch.setattr(daily_update.tpex_client, "fetch_company_directory", lambda: [])

    code, error = daily_update._resolve_symbol_input("台積")

    assert code is None
    assert "台積電" in error and "台積寶" in error


def test_resolve_symbol_input_reports_not_found(monkeypatch):
    monkeypatch.setattr(daily_update.twse_client, "fetch_company_directory", lambda: [])
    monkeypatch.setattr(daily_update.tpex_client, "fetch_company_directory", lambda: [])

    code, error = daily_update._resolve_symbol_input("不存在的公司名稱")

    assert code is None
    assert "不存在的公司名稱" in error


def test_resolve_symbol_input_falls_back_to_twse_when_tpex_directory_fails(monkeypatch):
    # TPEx的SSL已知偶爾不穩定(跟run_batch.py同樣的問題)——TPEx連線失敗不該讓整個名稱
    # 解析當掉，只要TWSE那邊查得到一樣要能成功解析
    monkeypatch.setattr(
        daily_update.twse_client, "fetch_company_directory", lambda: [{"symbol": "2317", "name": "鴻海"}]
    )

    def raise_ssl_error():
        raise requests.exceptions.SSLError("certificate verify failed")

    monkeypatch.setattr(daily_update.tpex_client, "fetch_company_directory", raise_ssl_error)

    assert daily_update._resolve_symbol_input("鴻海") == ("2317", "")


def test_resolve_symbol_input_reports_clear_error_when_both_directories_fail(monkeypatch):
    def raise_error():
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(daily_update.twse_client, "fetch_company_directory", raise_error)
    monkeypatch.setattr(daily_update.tpex_client, "fetch_company_directory", raise_error)

    code, error = daily_update._resolve_symbol_input("鴻海")

    assert code is None
    assert "TWSE" in error or "連線" in error


def test_add_symbol_to_watchlist_accepts_chinese_name(tmp_path, monkeypatch):
    """整合測試：新增時打中文名稱，應該解析成代號後走跟平常打代號完全一樣的流程。"""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    config = make_config(db_path)

    monkeypatch.setattr(
        daily_update.twse_client, "fetch_company_directory", lambda: [{"symbol": "2408", "name": "南亞科"}]
    )
    monkeypatch.setattr(daily_update.tpex_client, "fetch_company_directory", lambda: [])

    bar = Bar(symbol="2408", ts=datetime(2026, 8, 7), open=10, high=11, low=9, close=10, volume=100)
    monkeypatch.setattr(daily_update, "detect_market_and_fetch_bars", lambda code, period: ([bar], "TWSE"))
    monkeypatch.setattr(daily_update.finmind_client, "fetch_institutional_flows_for_range", lambda s, a, b: [])
    monkeypatch.setattr(daily_update.finmind_client, "fetch_valuations_for_range", lambda s, a, b: [])
    monkeypatch.setattr(daily_update.finmind_client, "fetch_monthly_revenue_for_range", lambda s, a, b: [])
    monkeypatch.setattr(
        daily_update.twse_client,
        "fetch_valuations_for_date",
        lambda d: [{"symbol": "2408", "name": "南亞科", "date": d, "pe_ratio": 10, "dividend_yield": 1, "pb_ratio": 1}],
    )

    result = daily_update.add_symbol_to_watchlist(config, "南亞科")

    assert result["ok"] is True
    with connect(db_path) as conn:
        row = conn.execute("SELECT code, name FROM symbols WHERE code = '2408'").fetchone()
    assert row["code"] == "2408"
    assert row["name"] == "南亞科"


def test_add_symbol_to_watchlist_reports_unresolvable_chinese_name(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    config = make_config(db_path)

    monkeypatch.setattr(daily_update.twse_client, "fetch_company_directory", lambda: [])
    monkeypatch.setattr(daily_update.tpex_client, "fetch_company_directory", lambda: [])

    result = daily_update.add_symbol_to_watchlist(config, "亂打的名字")

    assert result["ok"] is False
    assert "亂打的名字" in result["message"]


def test_refresh_market_data_tpex_queries_from_day_after_last_synced_date(tmp_path, monkeypatch):
    """已經有籌碼資料的上櫃股票，increment查詢範圍該是「上次抓到的日期+1」~「今天」，
    不是每次都重新抓整段3年歷史(那樣沒必要，institutional_flows已經有的資料還是會被
    INSERT OR REPLACE蓋掉，只是白白浪費一次API呼叫)。"""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    config = make_config(db_path)

    with connect(db_path) as conn:
        add_to_watchlist(conn, "8299", market="TPEx")
        conn.execute(
            "INSERT INTO institutional_flows (symbol, date, foreign_net, trust_net, dealer_net, total_net) "
            "VALUES ('8299', '2026-08-01', 0, 0, 0, 0)"
        )

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls):
            return datetime(2026, 8, 7)

    monkeypatch.setattr(daily_update, "datetime", FrozenDateTime)
    monkeypatch.setattr(daily_update.finmind_client, "fetch_valuations_for_range", lambda s, a, b: [])
    monkeypatch.setattr(daily_update.finmind_client, "fetch_monthly_revenue_for_range", lambda s, a, b: [])
    monkeypatch.setattr(daily_update.tpex_client, "fetch_ex_dividend_schedule", lambda: [])

    captured = {}

    def fake_flows(symbol, start_date, end_date):
        captured["args"] = (symbol, start_date, end_date)
        return []

    monkeypatch.setattr(daily_update.finmind_client, "fetch_institutional_flows_for_range", fake_flows)

    daily_update._refresh_market_data_tpex(config, {"8299"})

    assert captured["args"] == ("8299", "2026-08-02", "2026-08-07")


def test_refresh_market_data_tpex_skips_symbol_already_synced_today(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    config = make_config(db_path)

    with connect(db_path) as conn:
        add_to_watchlist(conn, "8299", market="TPEx")
        conn.execute(
            "INSERT INTO institutional_flows (symbol, date, foreign_net, trust_net, dealer_net, total_net) "
            "VALUES ('8299', '2026-08-07', 0, 0, 0, 0)"
        )

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls):
            return datetime(2026, 8, 7)

    monkeypatch.setattr(daily_update, "datetime", FrozenDateTime)
    monkeypatch.setattr(daily_update.finmind_client, "fetch_valuations_for_range", lambda s, a, b: [])
    monkeypatch.setattr(daily_update.finmind_client, "fetch_monthly_revenue_for_range", lambda s, a, b: [])
    monkeypatch.setattr(daily_update.tpex_client, "fetch_ex_dividend_schedule", lambda: [])

    calls = []
    monkeypatch.setattr(
        daily_update.finmind_client, "fetch_institutional_flows_for_range", lambda *a: calls.append(a)
    )

    daily_update._refresh_market_data_tpex(config, {"8299"})

    assert calls == [], "已經抓過今天的資料，不該再打一次FinMind"


def test_refresh_market_data_tpex_backfills_valuation_via_finmind(tmp_path, monkeypatch):
    """2026-08-13改用FinMind：估值也改成跟三大法人同一套「上次抓到的日期+1」~
    「今天」範圍查詢，不再打TPEx官方API(www.tpex.org.tw常有SSL憑證問題)。"""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    config = make_config(db_path)

    with connect(db_path) as conn:
        add_to_watchlist(conn, "8299", market="TPEx")

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls):
            return datetime(2026, 8, 7)

    monkeypatch.setattr(daily_update, "datetime", FrozenDateTime)
    monkeypatch.setattr(daily_update.finmind_client, "fetch_institutional_flows_for_range", lambda s, a, b: [])
    monkeypatch.setattr(daily_update.tpex_client, "fetch_ex_dividend_schedule", lambda: [])

    monkeypatch.setattr(
        daily_update.finmind_client,
        "fetch_valuations_for_range",
        lambda s, a, b: [{"symbol": s, "date": b, "pe_ratio": 15.0, "dividend_yield": 2.0, "pb_ratio": 3.0}],
    )

    daily_update._refresh_market_data_tpex(config, {"8299"})

    with connect(db_path) as conn:
        valuation_row = conn.execute("SELECT * FROM valuations WHERE symbol = '8299'").fetchone()
    assert valuation_row["pe_ratio"] == 15.0


def test_refresh_market_data_tpex_valuation_failure_does_not_block_flows(tmp_path, monkeypatch):
    """兩個FinMind資料源各自獨立try/except：估值連線失敗不該讓三大法人那邊
    已經抓到的資料白白浪費，也不該讓整個函式拋出例外。"""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    config = make_config(db_path)

    with connect(db_path) as conn:
        add_to_watchlist(conn, "8299", market="TPEx")

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls):
            return datetime(2026, 8, 7)

    monkeypatch.setattr(daily_update, "datetime", FrozenDateTime)
    monkeypatch.setattr(daily_update.tpex_client, "fetch_ex_dividend_schedule", lambda: [])
    monkeypatch.setattr(
        daily_update.finmind_client,
        "fetch_institutional_flows_for_range",
        lambda s, a, b: [{"symbol": s, "date": b, "foreign_net": 100, "trust_net": 50, "dealer_net": 0, "total_net": 150}],
    )

    def raise_ssl_error(symbol, start_date, end_date):
        raise requests.exceptions.SSLError("Missing Subject Key Identifier")

    monkeypatch.setattr(daily_update.finmind_client, "fetch_valuations_for_range", raise_ssl_error)

    daily_update._refresh_market_data_tpex(config, {"8299"})

    with connect(db_path) as conn:
        flow_row = conn.execute("SELECT * FROM institutional_flows WHERE symbol = '8299'").fetchone()
        valuation_row = conn.execute("SELECT * FROM valuations WHERE symbol = '8299'").fetchone()
    assert flow_row["foreign_net"] == 100
    assert valuation_row is None


def test_should_check_for_updates_true_when_never_checked_before():
    assert daily_update.should_check_for_updates(None, datetime(2026, 8, 7, 9, 0)) is True


def test_should_check_for_updates_true_on_a_new_calendar_day():
    last_check = datetime(2026, 8, 6, 20, 0)  # checked late yesterday
    now = datetime(2026, 8, 7, 9, 0)  # first open today
    assert daily_update.should_check_for_updates(last_check, now) is True


def test_should_check_for_updates_false_when_already_checked_today_before_cutoff():
    # checked once this morning; refreshing again before the EOD-data cutoff shouldn't
    # re-hit the API since nothing new could possibly have arrived yet
    last_check = datetime(2026, 8, 7, 9, 0)
    now = datetime(2026, 8, 7, 14, 0)
    assert daily_update.should_check_for_updates(last_check, now) is False


def test_should_check_for_updates_true_once_past_cutoff_if_last_check_was_before_it():
    # last check was this morning (before 19:00); it's now past 19:00, so today's freshly
    # published EOD data hasn't been checked for yet -- this is the one re-check per day
    last_check = datetime(2026, 8, 7, 9, 0)
    now = datetime(2026, 8, 7, 20, 0)
    assert daily_update.should_check_for_updates(last_check, now) is True


def test_should_check_for_updates_false_when_already_checked_again_after_cutoff():
    # already did the post-cutoff re-check; a later refresh the same evening shouldn't
    # trigger yet another one
    last_check = datetime(2026, 8, 7, 19, 30)
    now = datetime(2026, 8, 7, 21, 0)
    assert daily_update.should_check_for_updates(last_check, now) is False


def test_is_market_open_now_true_during_trading_hours():
    config = make_config("unused.db")  # market_open="09:00", market_close="13:30"
    assert daily_update.is_market_open_now(config, datetime(2026, 8, 7, 10, 0)) is True  # 2026-08-07是週五


def test_is_market_open_now_true_at_exact_open_and_close_boundary():
    config = make_config("unused.db")
    assert daily_update.is_market_open_now(config, datetime(2026, 8, 7, 9, 0)) is True
    assert daily_update.is_market_open_now(config, datetime(2026, 8, 7, 13, 30)) is True


def test_is_market_open_now_false_before_open_or_after_close():
    config = make_config("unused.db")
    assert daily_update.is_market_open_now(config, datetime(2026, 8, 7, 8, 59)) is False
    assert daily_update.is_market_open_now(config, datetime(2026, 8, 7, 13, 31)) is False


def test_is_market_open_now_false_on_weekend():
    # 2026-08-07是週五收盤時間，2026-08-08是隔天週六——同樣的時刻週末該回傳False
    config = make_config("unused.db")
    assert daily_update.is_market_open_now(config, datetime(2026, 8, 8, 10, 0)) is False  # 週六
    assert daily_update.is_market_open_now(config, datetime(2026, 8, 9, 10, 0)) is False  # 週日
