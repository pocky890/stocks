from dataclasses import replace
from datetime import datetime

import pandas as pd
import pytest

from stocks import telegram_client
from stocks.config import Config
from stocks.models import Direction, SignalEvent, Tier
from stocks.notifier import notify_batch_summary, notify_connectivity, notify_symbol_signals

EMPTY_BARS = pd.DataFrame(columns=["close"])


def make_daily_bars(closes):
    dates = pd.date_range("2026-01-01", periods=len(closes), freq="D")
    return pd.DataFrame({"close": closes}, index=dates)


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


def make_event(strategy, direction, detail="", ts=None):
    return SignalEvent(
        symbol="2330",
        strategy=strategy,
        direction=direction,
        price=600.0,
        ts=ts or datetime.now(),
        detail=detail,
        tier=Tier.REALTIME,
    )


def test_notify_symbol_signals_combines_all_strategies_into_one_message(captured_calls):
    config = make_config()
    events = [
        make_event("ma_crossover", Direction.BUY, "golden cross"),
        make_event("rsi", Direction.BUY, "RSI跌破30"),
    ]
    notify_symbol_signals(config, "2330", "台積電", events, EMPTY_BARS)

    assert len(captured_calls) == 1, "multiple triggered strategies for one symbol must be one message"
    text = captured_calls[0]["data"]["text"]
    assert "[V] golden cross" in text and "[V] RSI跌破30" in text
    assert "2330 台積電" in text
    assert "$600.0" in text
    assert "🟢" in text, "全部都是買進訊號，標頭要是進場觸發"


def test_notify_symbol_signals_uses_warning_header_when_all_sell(captured_calls):
    config = make_config()
    events = [make_event("macd", Direction.SELL, "MACD死亡交叉"), make_event("kd", Direction.SELL, "KD高檔死亡交叉")]
    notify_symbol_signals(config, "2344", "華邦電", events, EMPTY_BARS)

    text = captured_calls[0]["data"]["text"]
    assert "🔴" in text and "警戒" in text


def test_notify_symbol_signals_falls_back_to_symbol_when_name_missing(captured_calls):
    config = make_config()
    events = [make_event("macd", Direction.BUY, "MACD黃金交叉"), make_event("rsi", Direction.BUY, "RSI跌破30")]
    notify_symbol_signals(config, "2330", "", events, EMPTY_BARS)

    text = captured_calls[0]["data"]["text"]
    assert "標的：2330\n" in text, "沒有股票名稱時不該印出多一個空白"


def test_notify_symbol_signals_sends_nothing_for_empty_events(captured_calls):
    config = make_config()
    notify_symbol_signals(config, "2330", "台積電", [], EMPTY_BARS)
    assert len(captured_calls) == 0


def test_notify_symbol_signals_suppresses_single_strategy_trigger(captured_calls):
    """單一指標誤判率較高，同一時間點沒有別的策略同方向confirm，就不推播到Telegram
    (但DB仍會照樣記錄這個事件——這裡測的是通知層，不是資料庫層)。"""
    config = make_config()
    notify_symbol_signals(config, "2330", "台積電", [make_event("macd", Direction.BUY, "MACD黃金交叉")], EMPTY_BARS)
    assert len(captured_calls) == 0


def test_notify_symbol_signals_does_not_treat_conflicting_directions_as_confirmation(captured_calls):
    """1個買進+1個賣出，兩個方向互相矛盾，不該加總湊到門檻——各自都只有1個，
    都不足以confirm，整批都不推播。"""
    config = make_config()
    events = [make_event("macd", Direction.BUY, "MACD黃金交叉"), make_event("kd", Direction.SELL, "KD高檔死亡交叉")]
    notify_symbol_signals(config, "2330", "台積電", events, EMPTY_BARS)
    assert len(captured_calls) == 0


def test_notify_symbol_signals_includes_trend_line_when_daily_bars_available(captured_calls):
    """趨勢(站上/跌破哪些均線)是daily_bars隨時能算的當下狀態，不是編造的分數，
    所以額外加一行，格式跟以前dashboard的多空排列欄位一致(月線=20日,季線=60日)。"""
    config = make_config()
    uptrend = make_daily_bars([100 + i * 0.5 for i in range(70)])  # 持續上漲70天，現價必然站上所有均線
    events = [make_event("macd", Direction.BUY, "MACD黃金交叉"), make_event("rsi", Direction.BUY, "RSI跌破30")]
    notify_symbol_signals(config, "2330", "台積電", events, uptrend)

    text = captured_calls[0]["data"]["text"]
    assert "📈 趨勢：站上5、10日線、月、季線" in text


def test_notify_symbol_signals_omits_trend_line_when_daily_bars_missing(captured_calls):
    config = make_config()
    events = [make_event("macd", Direction.BUY, "MACD黃金交叉"), make_event("rsi", Direction.BUY, "RSI跌破30")]
    notify_symbol_signals(config, "2330", "台積電", events, EMPTY_BARS)

    text = captured_calls[0]["data"]["text"]
    assert "趨勢" not in text, "沒有日線資料(例如新股票剛加進來)就不該印出趨勢那一行"


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
        make_event("ma_crossover", Direction.BUY, "golden cross"),
        make_event("macd", Direction.BUY, "MACD黃金交叉"),
        SignalEvent("2317", "rsi", Direction.SELL, 100.0, datetime.now(), tier=Tier.BATCH, detail="RSI突破70"),
        SignalEvent("2317", "kd", Direction.SELL, 100.0, datetime.now(), tier=Tier.BATCH, detail="KD高檔死亡交叉"),
    ]
    notify_batch_summary(config, events)

    assert len(captured_calls) == 1
    text = captured_calls[0]["data"]["text"]
    assert "2330" in text and "2317" in text
    assert "共 2 檔" in text
    assert "@600.0" in text and "@100.0" in text, "有價格才看得出這是不是值得看的訊號"


def test_notify_batch_summary_drops_symbols_with_only_one_confirming_strategy(captured_calls):
    """同一檔股票當天只有1個策略觸發，跟notify_symbol_signals一樣的門檻，不夠格上摘要。"""
    config = make_config()
    events = [
        make_event("ma_crossover", Direction.BUY, "golden cross"),  # 2330只有1個，會被濾掉
        SignalEvent("2317", "rsi", Direction.SELL, 100.0, datetime.now(), tier=Tier.BATCH, detail="RSI突破70"),
        SignalEvent("2317", "kd", Direction.SELL, 100.0, datetime.now(), tier=Tier.BATCH, detail="KD高檔死亡交叉"),
    ]
    notify_batch_summary(config, events)

    text = captured_calls[0]["data"]["text"]
    assert "2317" in text and "共 1 檔" in text
    assert "2330" not in text, "2330只有1個策略觸發，不該出現在摘要裡"


def test_notify_batch_summary_filters_out_historical_backlog(captured_calls):
    """edge-triggered策略每次都重新掃過整段歷史，第一次幫某檔股票建訊號表(或排程斷過
    幾天)時，很久以前的crossing也會被db.insert_signal_events()當成「新的」一起插入，
    但摘要只該通知今天真的觸發的，不然會被歷史累積灌爆(regression: 曾一次收到769檔全是舊資料)。"""
    config = make_config()
    old_event = make_event("macd", Direction.BUY, "MACD黃金交叉", ts=datetime(2020, 1, 1))
    today_event = make_event("rsi", Direction.SELL, "RSI突破70", ts=datetime.now())
    today_event2 = make_event("kd", Direction.SELL, "KD高檔死亡交叉", ts=datetime.now())

    notify_batch_summary(config, [old_event, today_event, today_event2])

    assert len(captured_calls) == 1
    text = captured_calls[0]["data"]["text"]
    assert "共 1 檔" in text
    assert "MACD黃金交叉" not in text, "很久以前的舊訊號不該出現在今天的摘要裡"


def test_notify_batch_summary_says_nothing_new_when_all_events_are_historical(captured_calls):
    config = make_config()
    old_event = make_event("macd", Direction.BUY, ts=datetime(2020, 1, 1))

    notify_batch_summary(config, [old_event])

    assert len(captured_calls) == 1
    assert "今天沒有符合條件的股票" in captured_calls[0]["data"]["text"]


def test_send_message_without_credentials_does_not_call_requests(captured_calls):
    config = make_config(telegram_bot_token="", telegram_chat_id="")
    events = [make_event("ma_crossover", Direction.BUY), make_event("rsi", Direction.BUY)]
    notify_symbol_signals(config, "2330", "台積電", events, EMPTY_BARS)
    assert len(captured_calls) == 0
