from dataclasses import replace
from datetime import datetime

import pytest

from stocks import telegram_client
from stocks.config import Config
from stocks.models import Direction, SignalEvent, Tier
from stocks.notifier import notify_batch_summary, notify_connectivity, notify_symbol_signals


def make_config(**overrides) -> Config:
    base = dict(
        shioaji_api_key="",
        shioaji_secret_key="",
        telegram_bot_token="fake-token",
        telegram_chat_id="12345",
        market_open="09:00",
        market_close="13:30",
        bar_interval_minutes=5,
        batch_pacing_seconds=0.5,
        strategy_params={},
        db_path=":memory:",
    )
    base.update(overrides)
    return Config(**base)


class FakeResponse:
    def raise_for_status(self):
        pass


@pytest.fixture
def captured_calls(monkeypatch):
    calls = []

    def fake_post(url, data, timeout):
        calls.append({"url": url, "data": data, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(telegram_client.requests, "post", fake_post)
    return calls


def make_event(strategy, direction, detail=""):
    return SignalEvent(
        symbol="2330",
        strategy=strategy,
        direction=direction,
        price=600.0,
        ts=datetime(2026, 1, 5, 10, 35),
        detail=detail,
        tier=Tier.REALTIME,
    )


def test_notify_symbol_signals_combines_all_strategies_into_one_message(captured_calls):
    config = make_config()
    events = [
        make_event("ma_crossover", Direction.BUY, "golden cross"),
        make_event("rsi", Direction.BUY, "RSI跌破30"),
    ]
    notify_symbol_signals(config, "2330", events)

    assert len(captured_calls) == 1, "multiple triggered strategies for one symbol must be one message"
    text = captured_calls[0]["data"]["text"]
    assert "ma_crossover" in text and "rsi" in text
    assert "2330" in text


def test_notify_symbol_signals_sends_nothing_for_empty_events(captured_calls):
    config = make_config()
    notify_symbol_signals(config, "2330", [])
    assert len(captured_calls) == 0


def test_notify_connectivity_lost_and_restored(captured_calls):
    config = make_config()
    notify_connectivity(config, "lost", "Shioaji無回應超過60秒")
    notify_connectivity(config, "restored")

    assert len(captured_calls) == 2
    assert "連線中斷" in captured_calls[0]["data"]["text"]
    assert "連線已恢復" in captured_calls[1]["data"]["text"]


def test_notify_batch_summary_groups_by_symbol(captured_calls):
    config = make_config()
    events = [
        make_event("ma_crossover", Direction.BUY),
        SignalEvent("2317", "rsi", Direction.SELL, 100.0, datetime(2026, 1, 5), tier=Tier.BATCH),
    ]
    notify_batch_summary(config, events)

    assert len(captured_calls) == 1
    text = captured_calls[0]["data"]["text"]
    assert "2330" in text and "2317" in text
    assert "共 2 檔" in text


def test_send_message_without_credentials_does_not_call_requests(captured_calls):
    config = make_config(telegram_bot_token="", telegram_chat_id="")
    notify_symbol_signals(config, "2330", [make_event("ma_crossover", Direction.BUY)])
    assert len(captured_calls) == 0
