from datetime import datetime

import pandas as pd
import pytest

from stocks import telegram_client
from stocks.config import Config
from stocks.models import Direction, SignalEvent, Tier
from stocks.notifier import (
    notify_batch_summary,
    notify_connectivity,
    notify_ex_dividend_today,
    notify_reminder,
    notify_symbol_signals,
)

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


def make_event(strategy, direction, detail="", symbol="2330", ts=None):
    return SignalEvent(
        symbol=symbol,
        strategy=strategy,
        direction=direction,
        price=600.0,
        ts=ts or datetime.now(),
        detail=detail,
        tier=Tier.REALTIME,
    )


def test_notify_symbol_signals_sends_for_a_single_notifiable_trigger(captured_calls):
    """NOTIFIABLE_STRATEGIES(atr_breakout/chip_momentum/trend_following/breakout)本身
    已經是進出場邏輯完整的策略，觸發1次就該通知，不需要像單一指標那樣等別的策略一起confirm。"""
    config = make_config()
    events = [make_event("atr_breakout", Direction.BUY, "創20日新高突破，ATR停損 580.00")]
    notify_symbol_signals(config, "2330", "台積電", events, EMPTY_BARS)

    assert len(captured_calls) == 1
    text = captured_calls[0]["data"]["text"]
    assert "[V] ATR動態通道突破：創20日新高突破，ATR停損 580.00" in text, "要標明是哪個策略觸發的，跟dashboard上的策略名稱一致"
    assert "2330 台積電" in text
    assert "$600.0" in text
    assert "🟢" in text


def test_notify_symbol_signals_ignores_single_indicator_strategies(captured_calls):
    """RSI/MACD/KD這些單一指標不再各自觸發通知(只有NOTIFIABLE_STRATEGIES會)，
    這裡完全沒有NOTIFIABLE_STRATEGIES事件，就不該推播。"""
    config = make_config()
    events = [make_event("rsi", Direction.BUY, "RSI跌破30"), make_event("macd", Direction.BUY, "MACD黃金交叉")]
    notify_symbol_signals(config, "2330", "台積電", events, EMPTY_BARS)
    assert len(captured_calls) == 0


def test_notify_symbol_signals_only_shows_notifiable_events_when_mixed_with_single_indicators(captured_calls):
    """同一批events裡混了單一指標(rsi)跟策略(atr_breakout)，訊息裡只該列出
    策略那一項，單一指標不該出現(它們已經不算「觸發訊號」)。"""
    config = make_config()
    events = [
        make_event("rsi", Direction.BUY, "RSI跌破30"),
        make_event("atr_breakout", Direction.BUY, "創20日新高突破，ATR停損 580.00"),
    ]
    notify_symbol_signals(config, "2330", "台積電", events, EMPTY_BARS)

    assert len(captured_calls) == 1
    text = captured_calls[0]["data"]["text"]
    assert "創20日新高突破" in text
    assert "RSI跌破30" not in text
    assert "觸發訊號（1項）" in text


def test_notify_symbol_signals_uses_warning_header_for_sell_events(captured_calls):
    config = make_config()
    events = [make_event("chip_momentum", Direction.SELL, "跌破ATR移動停損 620.00")]
    notify_symbol_signals(config, "2344", "華邦電", events, EMPTY_BARS)

    text = captured_calls[0]["data"]["text"]
    assert "🔴" in text and "警戒" in text


def test_notify_symbol_signals_falls_back_to_symbol_when_name_missing(captured_calls):
    config = make_config()
    events = [make_event("atr_breakout", Direction.BUY, "創20日新高突破，ATR停損 580.00")]
    notify_symbol_signals(config, "2330", "", events, EMPTY_BARS)

    text = captured_calls[0]["data"]["text"]
    assert "標的：2330\n" in text, "沒有股票名稱時不該印出多一個空白"


def test_notify_symbol_signals_sends_nothing_for_empty_events(captured_calls):
    config = make_config()
    notify_symbol_signals(config, "2330", "台積電", [], EMPTY_BARS)
    assert len(captured_calls) == 0


def test_notify_symbol_signals_includes_trend_line_when_daily_bars_available(captured_calls):
    """趨勢(站上/跌破哪些均線)是daily_bars隨時能算的當下狀態，不是編造的分數，
    所以額外加一行，格式跟以前dashboard的多空排列欄位一致(月線=20日,季線=60日)。"""
    config = make_config()
    uptrend = make_daily_bars([100 + i * 0.5 for i in range(70)])  # 持續上漲70天，現價必然站上所有均線
    events = [make_event("atr_breakout", Direction.BUY, "創20日新高突破，ATR停損 580.00")]
    notify_symbol_signals(config, "2330", "台積電", events, uptrend)

    text = captured_calls[0]["data"]["text"]
    assert "📈 趨勢：站上5、10日線、月、季線" in text


def test_notify_symbol_signals_omits_trend_line_when_daily_bars_missing(captured_calls):
    config = make_config()
    events = [make_event("atr_breakout", Direction.BUY, "創20日新高突破，ATR停損 580.00")]
    notify_symbol_signals(config, "2330", "台積電", events, EMPTY_BARS)

    text = captured_calls[0]["data"]["text"]
    assert "趨勢" not in text, "沒有日線資料(例如新股票剛加進來)就不該印出趨勢那一行"


def test_notify_symbol_signals_shows_entry_info_and_return_for_sell(captured_calls):
    # 2026-08-19使用者要求出場通知要看得到進場日期/價位跟報酬率，不能只看到出場原因。
    config = make_config()
    events = [
        SignalEvent(
            "6531", "long_swing", Direction.SELL, 851.0, datetime(2026, 8, 19, 9, 5), tier=Tier.REALTIME, detail="連續3天跌破60日均線"
        )
    ]
    entry_events = {"long_swing": {"ts": "2026-08-01T09:30:00", "price": 820.0}}
    notify_symbol_signals(config, "6531", "愛普", events, EMPTY_BARS, entry_events=entry_events)

    text = captured_calls[0]["data"]["text"]
    assert "進場：2026-08-01 @820.0 → 出場@851.0，報酬率：+3.8%" in text


def test_notify_symbol_signals_omits_entry_info_when_no_prior_entry_found(captured_calls):
    # 找不到對應進場紀錄(例如這個(symbol,strategy)第一次出場)就只顯示出場原因，不能
    # 因為缺這段資訊擋掉整則通知。
    config = make_config()
    events = [make_event("long_swing", Direction.SELL, "連續3天跌破60日均線")]
    notify_symbol_signals(config, "6531", "愛普", events, EMPTY_BARS, entry_events={})

    text = captured_calls[0]["data"]["text"]
    assert "連續3天跌破60日均線" in text
    assert "進場：" not in text


def test_notify_symbol_signals_omits_entry_info_for_buy_events(captured_calls):
    # 進場資訊只對出場(SELL)有意義，買進通知本身就是「現在進場」，不該附加報酬率這段。
    config = make_config()
    events = [make_event("long_swing", Direction.BUY, "多空排列站上均線")]
    entry_events = {"long_swing": {"ts": "2026-08-01T09:30:00", "price": 820.0}}
    notify_symbol_signals(config, "6531", "愛普", events, EMPTY_BARS, entry_events=entry_events)

    text = captured_calls[0]["data"]["text"]
    assert "進場：" not in text


def test_notify_symbol_signals_warns_when_sell_falls_on_ex_dividend_date(captured_calls):
    # 2026-08-15使用者發現：除權息當天股價會被交易所機制性扣掉股利金額，停損邏輯看不出
    # 這是股息因素還是真的下跌，容易誤判——賣出訊號剛好落在已知的除權息日就該額外提醒。
    config = make_config()
    events = [make_event("chip_momentum", Direction.SELL, "跌破15%移動停損 100.00", ts=datetime(2026, 1, 6, 13, 20))]
    notify_symbol_signals(config, "2330", "台積電", events, EMPTY_BARS, ex_dividend_dates={"2026-01-06"})

    text = captured_calls[0]["data"]["text"]
    assert "除權息" in text
    assert "2026-01-06" in text


def test_notify_symbol_signals_omits_ex_dividend_warning_when_date_does_not_match(captured_calls):
    config = make_config()
    events = [make_event("chip_momentum", Direction.SELL, "跌破15%移動停損 100.00", ts=datetime(2026, 1, 6, 13, 20))]
    notify_symbol_signals(config, "2330", "台積電", events, EMPTY_BARS, ex_dividend_dates={"2026-03-18"})

    text = captured_calls[0]["data"]["text"]
    assert "除權息" not in text


def test_notify_symbol_signals_omits_ex_dividend_warning_for_buy_only_events(captured_calls):
    # 除權息造成的機制性下跌只會誤判賣出訊號，買進訊號跟這個無關，就算當天剛好是除權息日
    # 也不該印出提醒(避免使用者誤以為買進訊號也受影響)。
    config = make_config()
    events = [make_event("chip_momentum", Direction.BUY, "外資連3日買超", ts=datetime(2026, 1, 6, 13, 20))]
    notify_symbol_signals(config, "2330", "台積電", events, EMPTY_BARS, ex_dividend_dates={"2026-01-06"})

    text = captured_calls[0]["data"]["text"]
    assert "除權息" not in text


def test_notify_reminder_describes_sell_signal_still_below_trigger(captured_calls):
    # 2026-08-14使用者要求：9:30跌破ATR停損發過通知，13:20如果現價仍在觸發價之下(還沒
    # 回升)，代表狀況沒解除，要再提醒一次。使用者後來反饋看不出來是買還是賣，標題跟每
    # 一行都要清楚標示方向。
    config = make_config()
    row = {"strategy": "atr_breakout", "direction": "sell", "price": 100.0, "ts": "2026-08-14T09:30:00"}

    notify_reminder(config, "2330", "台積電", [row], current_price=95.0)

    assert len(captured_calls) == 1
    text = captured_calls[0]["data"]["text"]
    assert "賣出訊號還沒解除" in text, "標題要講清楚是買進還是賣出，不能只靠內文的動詞"
    assert "🔴賣" in text
    assert "還沒回升" in text
    assert "09:30" in text
    assert "ATR動態通道突破" in text, "要用中文策略名稱，跟其他通知一致"
    assert "$95.0" in text


def test_notify_reminder_describes_buy_signal_still_above_trigger(captured_calls):
    config = make_config()
    row = {"strategy": "breakout", "direction": "buy", "price": 100.0, "ts": "2026-08-14T09:30:00"}

    notify_reminder(config, "2330", "台積電", [row], current_price=105.0)

    text = captured_calls[0]["data"]["text"]
    assert "買進訊號還沒解除" in text
    assert "🟢買" in text
    assert "還沒跌破" in text


def test_notify_reminder_labels_title_when_buy_and_sell_both_still_pending(captured_calls):
    # 同一檔股票今天不同策略各自留了一個還沒解除的買進/賣出訊號，標題要講清楚兩種都有，
    # 不能只顯示其中一種方向
    config = make_config()
    rows = [
        {"strategy": "breakout", "direction": "buy", "price": 100.0, "ts": "2026-08-14T09:30:00"},
        {"strategy": "atr_breakout", "direction": "sell", "price": 200.0, "ts": "2026-08-14T10:00:00"},
    ]

    notify_reminder(config, "2330", "台積電", rows, current_price=105.0)

    text = captured_calls[0]["data"]["text"]
    assert "買進+賣出訊號都還沒解除" in text
    assert "🟢買" in text and "🔴賣" in text


def test_notify_reminder_shows_entry_info_and_return_for_sell(captured_calls):
    config = make_config()
    row = {"strategy": "long_swing", "direction": "sell", "price": 851.0, "ts": "2026-08-19T09:05:00"}
    entry_events = {"long_swing": {"ts": "2026-08-01T09:30:00", "price": 820.0}}

    notify_reminder(config, "6531", "愛普", [row], current_price=845.0, entry_events=entry_events)

    text = captured_calls[0]["data"]["text"]
    assert "進場：2026-08-01 @820.0，報酬率：+3.8%" in text


def test_notify_reminder_omits_entry_info_for_buy_rows(captured_calls):
    config = make_config()
    row = {"strategy": "breakout", "direction": "buy", "price": 100.0, "ts": "2026-08-14T09:30:00"}
    entry_events = {"breakout": {"ts": "2026-08-01T09:30:00", "price": 90.0}}

    notify_reminder(config, "2330", "台積電", [row], current_price=105.0, entry_events=entry_events)

    text = captured_calls[0]["data"]["text"]
    assert "進場：" not in text


def test_notify_reminder_sends_nothing_for_empty_rows(captured_calls):
    config = make_config()
    notify_reminder(config, "2330", "台積電", [], current_price=100.0)
    assert len(captured_calls) == 0


def test_notify_reminder_warns_when_today_is_ex_dividend_date(captured_calls):
    config = make_config()
    row = {"strategy": "atr_breakout", "direction": "sell", "price": 100.0, "ts": "2026-01-06T09:30:00"}

    notify_reminder(config, "2330", "台積電", [row], current_price=95.0, ex_dividend_dates={"2026-01-06"})

    text = captured_calls[0]["data"]["text"]
    assert "除權息" in text
    assert "2026-01-06" in text


def test_notify_reminder_omits_ex_dividend_warning_when_date_does_not_match(captured_calls):
    config = make_config()
    row = {"strategy": "atr_breakout", "direction": "sell", "price": 100.0, "ts": "2026-01-06T09:30:00"}

    notify_reminder(config, "2330", "台積電", [row], current_price=95.0, ex_dividend_dates={"2026-03-18"})

    text = captured_calls[0]["data"]["text"]
    assert "除權息" not in text


def test_notify_connectivity_lost_and_restored(captured_calls):
    config = make_config()
    notify_connectivity(config, "lost", "Shioaji無回應超過60秒")
    notify_connectivity(config, "restored")

    assert len(captured_calls) == 2
    assert "連線中斷" in captured_calls[0]["data"]["text"]
    assert "連線已恢復" in captured_calls[1]["data"]["text"]


def test_notify_batch_summary_lists_buy_events_for_symbols_outside_watchlist(captured_calls):
    """買進機會對誰都有意義，不管在不在觀察清單裡——watchlist在這裡應該完全不擋買進事件。"""
    config = make_config()
    events = [make_event("atr_breakout", Direction.BUY, "創20日新高突破，ATR停損 580.00", symbol="9999")]
    notify_batch_summary(config, events, watchlist=set())

    assert len(captured_calls) == 1
    text = captured_calls[0]["data"]["text"]
    assert "9999" in text and "共 1 檔" in text


def test_notify_batch_summary_suppresses_sell_events_outside_watchlist(captured_calls):
    """賣出訊號對「根本沒有在關注/沒有持有」的股票沒有意義，不在watchlist裡就不通知。"""
    config = make_config()
    events = [make_event("chip_momentum", Direction.SELL, "跌破ATR移動停損 620.00", symbol="9999")]
    notify_batch_summary(config, events, watchlist={"2330"})

    assert len(captured_calls) == 1
    assert "今天沒有符合條件的股票" in captured_calls[0]["data"]["text"]


def test_notify_batch_summary_allows_sell_events_inside_watchlist(captured_calls):
    config = make_config()
    events = [make_event("chip_momentum", Direction.SELL, "跌破ATR移動停損 620.00", symbol="2330")]
    notify_batch_summary(config, events, watchlist={"2330"})

    assert len(captured_calls) == 1
    text = captured_calls[0]["data"]["text"]
    assert "2330" in text and "共 1 檔" in text


def test_notify_batch_summary_ignores_single_indicator_strategies(captured_calls):
    config = make_config()
    events = [make_event("ma_crossover", Direction.BUY, "golden cross", symbol="2330")]
    notify_batch_summary(config, events, watchlist={"2330"})

    assert len(captured_calls) == 1
    assert "今天沒有符合條件的股票" in captured_calls[0]["data"]["text"]


def test_notify_batch_summary_groups_multiple_symbols(captured_calls):
    config = make_config()
    events = [
        make_event("atr_breakout", Direction.BUY, "創20日新高突破，ATR停損 580.00", symbol="2330"),
        SignalEvent("2317", "chip_momentum", Direction.SELL, 100.0, datetime.now(), tier=Tier.BATCH, detail="跌破ATR移動停損 100.00"),
    ]
    notify_batch_summary(config, events, watchlist={"2330", "2317"})

    assert len(captured_calls) == 1
    text = captured_calls[0]["data"]["text"]
    assert "2330" in text and "2317" in text
    assert "共 2 檔" in text
    assert "@600.0" in text and "@100.0" in text, "有價格才看得出這是不是值得看的訊號"


def test_notify_batch_summary_filters_out_historical_backlog(captured_calls):
    """edge-triggered策略每次都重新掃過整段歷史，第一次幫某檔股票建訊號表(或排程斷過
    幾天)時，很久以前的crossing也會被db.insert_signal_events()當成「新的」一起插入，
    但摘要只該通知今天真的觸發的，不然會被歷史累積灌爆(regression: 曾一次收到769檔全是舊資料)。"""
    config = make_config()
    old_event = make_event("atr_breakout", Direction.BUY, "創20日新高突破，ATR停損 580.00", ts=datetime(2020, 1, 1))
    today_event = make_event("chip_momentum", Direction.SELL, "跌破ATR移動停損 100.00", ts=datetime.now())

    notify_batch_summary(config, [old_event, today_event], watchlist={"2330"})

    assert len(captured_calls) == 1
    text = captured_calls[0]["data"]["text"]
    assert "共 1 檔" in text
    assert "創20日新高突破" not in text, "很久以前的舊訊號不該出現在今天的摘要裡"


def test_notify_batch_summary_says_nothing_new_when_all_events_are_historical(captured_calls):
    config = make_config()
    old_event = make_event("atr_breakout", Direction.BUY, ts=datetime(2020, 1, 1))

    notify_batch_summary(config, [old_event], watchlist={"2330"})

    assert len(captured_calls) == 1
    assert "今天沒有符合條件的股票" in captured_calls[0]["data"]["text"]


def test_notify_ex_dividend_today_lists_cash_dividend_amount(captured_calls):
    config = make_config()
    rows = [{"symbol": "2330", "name": "台積電", "cash_dividend": 4.5, "stock_dividend_ratio": None, "detail": "除息"}]

    notify_ex_dividend_today(config, rows)

    assert len(captured_calls) == 1
    text = captured_calls[0]["data"]["text"]
    assert "2330 台積電" in text
    assert "現金股利4.50元" in text
    assert "共 1 檔" in text


def test_notify_ex_dividend_today_lists_stock_dividend_ratio(captured_calls):
    config = make_config()
    rows = [{"symbol": "2330", "name": "台積電", "cash_dividend": None, "stock_dividend_ratio": 0.5, "detail": "除權"}]

    notify_ex_dividend_today(config, rows)

    text = captured_calls[0]["data"]["text"]
    assert "股票股利0.5" in text


def test_notify_ex_dividend_today_lists_multiple_symbols(captured_calls):
    config = make_config()
    rows = [
        {"symbol": "2330", "name": "台積電", "cash_dividend": 4.5, "stock_dividend_ratio": None, "detail": "除息"},
        {"symbol": "2454", "name": "聯發科", "cash_dividend": 10.0, "stock_dividend_ratio": None, "detail": "除息"},
    ]

    notify_ex_dividend_today(config, rows)

    text = captured_calls[0]["data"]["text"]
    assert "共 2 檔" in text
    assert "2330 台積電" in text
    assert "2454 聯發科" in text


def test_notify_ex_dividend_today_sends_nothing_when_no_symbols_today(captured_calls):
    config = make_config()
    notify_ex_dividend_today(config, [])
    assert len(captured_calls) == 0


def test_send_message_without_credentials_does_not_call_requests(captured_calls):
    config = make_config(telegram_bot_token="", telegram_chat_id="")
    events = [make_event("atr_breakout", Direction.BUY, "創20日新高突破，ATR停損 580.00")]
    notify_symbol_signals(config, "2330", "台積電", events, EMPTY_BARS)
    assert len(captured_calls) == 0
