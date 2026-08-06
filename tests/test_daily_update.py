from datetime import datetime

import requests

from stocks import daily_update
from stocks.config import Config
from stocks.db import add_to_watchlist, connect, init_db
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
