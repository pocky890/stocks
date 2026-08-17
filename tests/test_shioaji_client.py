import time
import types
from datetime import date, datetime

from stocks import shioaji_client as shioaji_client_module
from stocks.config import Config
from stocks.shioaji_client import ShioajiClient


def make_config() -> Config:
    return Config(
        shioaji_api_key="key",
        shioaji_secret_key="secret",
        telegram_bot_token="",
        telegram_chat_id="",
        market_open="09:00",
        market_close="13:30",
        bar_interval_minutes=5,
        batch_pacing_seconds=0,
        strategy_params={},
        db_path=":memory:",
    )


def test_ensure_connected_returns_true_immediately_when_already_connected(monkeypatch):
    client = ShioajiClient(make_config())
    client._connected = True
    monkeypatch.setattr(client, "connect", lambda: (_ for _ in ()).throw(AssertionError("shouldn't reconnect")))

    assert client.ensure_connected() is True


def test_ensure_connected_retries_with_backoff_then_succeeds(monkeypatch):
    client = ShioajiClient(make_config())
    client._connected = False
    attempts = []

    def fake_connect():
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("still down")
        client._connected = True

    monkeypatch.setattr(client, "connect", fake_connect)
    sleeps = []
    monkeypatch.setattr("stocks.shioaji_client.time.sleep", lambda s: sleeps.append(s))

    result = client.ensure_connected(max_retries=5, base_delay=1, max_delay=60)

    assert result is True
    assert len(attempts) == 3
    assert sleeps == [1, 2, 4], "exponential backoff: 1s, 2s, 4s before each retry"


def test_ensure_connected_gives_up_after_max_retries(monkeypatch):
    client = ShioajiClient(make_config())
    client._connected = False
    monkeypatch.setattr(client, "connect", lambda: (_ for _ in ()).throw(ConnectionError("still down")))
    monkeypatch.setattr("stocks.shioaji_client.time.sleep", lambda s: None)

    result = client.ensure_connected(max_retries=3, base_delay=1, max_delay=60)

    assert result is False


def test_ensure_connected_backoff_caps_at_max_delay(monkeypatch):
    client = ShioajiClient(make_config())
    client._connected = False
    monkeypatch.setattr(client, "connect", lambda: (_ for _ in ()).throw(ConnectionError("still down")))
    sleeps = []
    monkeypatch.setattr("stocks.shioaji_client.time.sleep", lambda s: sleeps.append(s))

    client.ensure_connected(max_retries=6, base_delay=1, max_delay=10)

    assert sleeps == [1, 2, 4, 8, 10, 10]


def test_on_session_down_flips_connected_flag():
    client = ShioajiClient(make_config())
    client._connected = True
    client._on_session_down()
    assert client._connected is False


class FakeShioajiApi:
    def __init__(self, login_delay: float = 0.0):
        self._login_delay = login_delay
        self.login_called_with = None
        self.session_down_callback = None

    def login(self, api_key, secret_key):
        self.login_called_with = (api_key, secret_key)
        if self._login_delay:
            time.sleep(self._login_delay)

    def on_session_down(self, callback):
        self.session_down_callback = callback


def test_connect_succeeds_and_sets_connected_flag_when_login_returns_quickly(monkeypatch):
    # 2026-08-17新增：connect()本來完全沒有直接測試過(其他測試都monkeypatch整個connect
    # 方法繞過它)，現在改成背景執行緒+逾時包裝，要驗證正常情況下行為不變。
    fake_api = FakeShioajiApi()
    monkeypatch.setattr(shioaji_client_module.sj, "Shioaji", lambda simulation: fake_api)
    client = ShioajiClient(make_config())

    client.connect()

    assert client._connected is True
    assert fake_api.login_called_with == ("key", "secret")
    assert fake_api.session_down_callback == client._on_session_down


def test_connect_raises_connection_error_when_login_hangs_past_timeout(monkeypatch):
    # 2026-08-17發現的bug：sj.Shioaji.login()網路異常時可能無限期卡住不回傳也不拋例外，
    # 這裡用一個「login會睡超過逾時時間」的假api模擬，驗證connect()改成拋
    # ConnectionError而不是讓呼叫端整個卡死——逾時時間設得很短，測試不用真的等15秒。
    fake_api = FakeShioajiApi(login_delay=1.0)
    monkeypatch.setattr(shioaji_client_module.sj, "Shioaji", lambda simulation: fake_api)
    monkeypatch.setattr(shioaji_client_module, "CONNECT_TIMEOUT_SECONDS", 0.05)
    client = ShioajiClient(make_config())

    try:
        client.connect()
        assert False, "應該要拋出ConnectionError，不是正常回傳"
    except ConnectionError as exc:
        assert "逾時" in str(exc)

    assert client._connected is False


def test_fetch_daily_quotes_parses_whole_market_into_bars():
    client = ShioajiClient(make_config())
    fake_quotes = {
        "Date": ["2026-08-05", "2026-08-05"],
        "Code": ["2330", "2317"],
        "Open": [2385.0, 100.0],
        "High": [2415.0, 105.0],
        "Low": [2370.0, 99.0],
        "Close": [2405.0, 102.0],
        "Volume": [36782301, 1000],
        "Transaction": [1, 1],
        "Amount": [1, 1],
    }
    client.api = types.SimpleNamespace(daily_quotes=lambda date: fake_quotes)

    bars = client.fetch_daily_quotes(date(2026, 8, 5))

    assert len(bars) == 2
    tsmc = next(b for b in bars if b.symbol == "2330")
    assert tsmc.close == 2405.0
    assert tsmc.volume == 36782301
    assert tsmc.ts == datetime(2026, 8, 5)


def test_fetch_daily_quotes_skips_rows_with_missing_ohlc():
    # 全天暫停交易的股票，Shioaji的daily_quotes會回傳缺值的OHLC(這裡用None模擬)，
    # 不是真正成交出來的K棒，插進bars_daily會讓rsi()等指標的.diff()整欄變成object dtype而crash
    client = ShioajiClient(make_config())
    fake_quotes = {
        "Date": ["2026-08-05", "2026-08-05"],
        "Code": ["2330", "1234"],
        "Open": [2385.0, None],
        "High": [2415.0, None],
        "Low": [2370.0, None],
        "Close": [2405.0, None],
        "Volume": [36782301, 0],
        "Transaction": [1, 1],
        "Amount": [1, 1],
    }
    client.api = types.SimpleNamespace(daily_quotes=lambda date: fake_quotes)

    bars = client.fetch_daily_quotes(date(2026, 8, 5))

    assert [b.symbol for b in bars] == ["2330"], "暫停交易(缺OHLC)的1234不該被當成一根K棒插進去"


def test_fetch_daily_quotes_returns_empty_list_when_market_closed():
    client = ShioajiClient(make_config())
    client.api = types.SimpleNamespace(daily_quotes=lambda date: {k: [] for k in ["Date", "Code", "Open", "High", "Low", "Close", "Volume"]})

    assert client.fetch_daily_quotes(date(2026, 8, 8)) == []


def test_fetch_today_kbars_skips_symbols_that_fail_but_keeps_others():
    client = ShioajiClient(make_config())

    def fake_fetch_kbars(symbol, start, end):
        if symbol == "9999":
            raise RuntimeError("bad symbol")
        return [{"fake": "bar"}] if symbol == "2330" else []

    client.fetch_kbars = fake_fetch_kbars

    result = client.fetch_today_kbars(["2330", "9999", "2317"])

    assert list(result.keys()) == ["2330"], "the failing symbol and the empty-bars symbol are both dropped"


def test_fetch_kbars_raises_connection_error_when_kbars_call_hangs_past_timeout(monkeypatch):
    # 2026-08-17發現的第二個卡死點：修完login()逾時後dashboard仍然完全卡死，查證是
    # fetch_today_kbars()逐檔呼叫kbars()這裡沒有任何逾時保護——同一種SDK缺陷，這裡驗證
    # fetch_kbars()改成拋ConnectionError而不是讓呼叫端卡死。
    client = ShioajiClient(make_config())

    def hanging_kbars(contract, start, end):
        time.sleep(1.0)
        return {}

    client.api = types.SimpleNamespace(
        Contracts=types.SimpleNamespace(Stocks={"2330": "fake-contract"}),
        kbars=hanging_kbars,
    )
    monkeypatch.setattr(shioaji_client_module, "KBARS_TIMEOUT_SECONDS", 0.05)

    try:
        client.fetch_kbars("2330", start="2026-08-01", end="2026-08-05")
        assert False, "應該要拋出ConnectionError，不是正常回傳"
    except ConnectionError as exc:
        assert "逾時" in str(exc)


def test_fetch_kbars_parses_per_symbol_history_into_bars():
    client = ShioajiClient(make_config())
    fake_kbars = {
        "ts": [1785700000000000000, 1785700060000000000],
        "Open": [2385.0, 2390.0],
        "High": [2390.0, 2395.0],
        "Low": [2380.0, 2385.0],
        "Close": [2390.0, 2392.0],
        "Volume": [100, 200],
    }
    client.api = types.SimpleNamespace(
        Contracts=types.SimpleNamespace(Stocks={"2330": "fake-contract"}),
        kbars=lambda contract, start, end: fake_kbars,
    )

    bars = client.fetch_kbars("2330", start="2026-08-01", end="2026-08-05")

    assert len(bars) == 2
    assert bars[0].symbol == "2330"
    assert bars[0].close == 2390.0
    assert bars[1].volume == 200
