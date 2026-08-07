from datetime import datetime

import requests

from stocks import daily_update
from stocks.config import Config
from stocks.db import add_to_watchlist, connect, get_disabled_strategies, init_db
from stocks.models import Bar


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

    result = daily_update.check_and_update(config)

    assert result["errors"] == []


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
    monkeypatch.setattr(daily_update.twse_client, "fetch_institutional_flows_for_date", lambda d: [])
    monkeypatch.setattr(daily_update.twse_client, "fetch_margin_balances_for_date", lambda d: [])

    result = daily_update.add_symbol_to_watchlist(config, "2408")

    assert result["ok"] is True
    assert "南亞科" in result["message"]
    with connect(db_path) as conn:
        row = conn.execute("SELECT name FROM symbols WHERE code = '2408'").fetchone()
    assert row["name"] == "南亞科"


def test_add_symbol_immediately_computes_disabled_strategies(tmp_path, monkeypatch):
    """新增股票不用等下個月的排程，當下就該幫它跑一次排除評估——資料通常還很少(只有
    1根K棒)，5個NOTIFIABLE_STRATEGIES都湊不出足夠的交易次數，理當全部不排除(空清單)，
    但disabled_strategies欄位本身要真的被寫入(不是None/從沒被touch過)，證明這條路徑
    真的執行了，不是被跳過。"""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    config = make_config(db_path)

    today = datetime.now()
    bar = Bar(symbol="2408", ts=today, open=10, high=11, low=9, close=10, volume=100)
    monkeypatch.setattr(daily_update, "detect_market_and_fetch_bars", lambda code, period: ([bar], "TWSE"))
    monkeypatch.setattr(daily_update.twse_client, "fetch_valuations_for_date", lambda d: [])
    monkeypatch.setattr(daily_update.twse_client, "fetch_institutional_flows_for_date", lambda d: [])
    monkeypatch.setattr(daily_update.twse_client, "fetch_margin_balances_for_date", lambda d: [])

    daily_update.add_symbol_to_watchlist(config, "2408")

    with connect(db_path) as conn:
        assert get_disabled_strategies(conn, "2408") == []


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
    monkeypatch.setattr(daily_update.tpex_client, "fetch_margin_balances_latest", lambda: [])
    monkeypatch.setattr(daily_update.tpex_client, "fetch_valuations_latest", lambda: [])

    captured = {}

    def fake_flows(symbol, start_date, end_date):
        captured["args"] = (symbol, start_date, end_date)
        return [{"symbol": symbol, "date": start_date, "foreign_net": 100, "trust_net": 50, "dealer_net": 0, "total_net": 150}]

    monkeypatch.setattr(daily_update.finmind_client, "fetch_institutional_flows_for_range", fake_flows)

    result = daily_update.add_symbol_to_watchlist(config, "8299")

    assert result["ok"] is True
    assert captured["args"] == ("8299", "2023-08-07", "2026-08-07")
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM institutional_flows WHERE symbol = '8299'").fetchone()
    assert row["foreign_net"] == 100


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
    monkeypatch.setattr(daily_update.tpex_client, "fetch_margin_balances_latest", lambda: [])
    monkeypatch.setattr(daily_update.tpex_client, "fetch_valuations_latest", lambda: [])
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
    monkeypatch.setattr(daily_update.tpex_client, "fetch_margin_balances_latest", lambda: [])
    monkeypatch.setattr(daily_update.tpex_client, "fetch_valuations_latest", lambda: [])
    monkeypatch.setattr(daily_update.tpex_client, "fetch_ex_dividend_schedule", lambda: [])

    calls = []
    monkeypatch.setattr(
        daily_update.finmind_client, "fetch_institutional_flows_for_range", lambda *a: calls.append(a)
    )

    daily_update._refresh_market_data_tpex(config, {"8299"})

    assert calls == [], "已經抓過今天的資料，不該再打一次FinMind"


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
