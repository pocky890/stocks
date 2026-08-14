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
    monkeypatch.setattr(daily_update.finmind_client, "fetch_margin_balances_for_range", lambda s, a, b: [])

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
    monkeypatch.setattr(daily_update.finmind_client, "fetch_margin_balances_for_range", lambda s, a, b: [])

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
    monkeypatch.setattr(daily_update.finmind_client, "fetch_margin_balances_for_range", lambda s, a, b: [])
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


def test_add_symbol_tpex_survives_valuation_ssl_failure(tmp_path, monkeypatch):
    """2026-08-13修正：新增上櫃股票時查估值(順便拿名稱)那段沒有try/except保護，
    www.tpex.org.tw常有的SSL憑證問題會讓整個新增股票crash——改成失敗時退回name=""，
    股票還是加得進觀察清單，只是暫時沒有名稱(下次每日更新查得到valuations時會補上)。"""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    config = make_config(db_path)

    bar = Bar(symbol="8299", ts=datetime(2026, 8, 7), open=10, high=11, low=9, close=10, volume=100)
    monkeypatch.setattr(daily_update, "detect_market_and_fetch_bars", lambda code, period: ([bar], "TPEx"))
    monkeypatch.setattr(daily_update.finmind_client, "fetch_institutional_flows_for_range", lambda s, a, b: [])
    monkeypatch.setattr(daily_update.finmind_client, "fetch_margin_balances_for_range", lambda s, a, b: [])

    def raise_ssl_error():
        raise requests.exceptions.SSLError("Missing Subject Key Identifier")

    monkeypatch.setattr(daily_update.tpex_client, "fetch_valuations_latest", raise_ssl_error)

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
    monkeypatch.setattr(daily_update.finmind_client, "fetch_margin_balances_for_range", lambda s, a, b: [])
    monkeypatch.setattr(
        daily_update.twse_client,
        "fetch_valuations_for_date",
        lambda d: [{"symbol": "2408", "name": "南亞科", "date": d, "pe_ratio": 10, "dividend_yield": 1, "pb_ratio": 1}],
    )

    captured = {}

    def fake_flows(symbol, start_date, end_date):
        captured["args"] = (symbol, start_date, end_date)
        return [{"symbol": symbol, "date": start_date, "foreign_net": 100, "trust_net": 50, "dealer_net": 0, "total_net": 150}]

    monkeypatch.setattr(daily_update.finmind_client, "fetch_institutional_flows_for_range", fake_flows)

    result = daily_update.add_symbol_to_watchlist(config, "2408")

    assert result["ok"] is True
    assert captured["args"] == ("2408", "2023-08-07", "2026-08-07")
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM institutional_flows WHERE symbol = '2408'").fetchone()
    assert row["foreign_net"] == 100


def test_add_symbol_twse_falls_back_to_latest_day_when_finmind_fails(tmp_path, monkeypatch):
    """FinMind額度用盡/連線失敗不該讓新增股票整個失敗——退回沒有三大法人歷史，之後每天
    累積即可，跟原本「上市只有最新一天」的舊行為效果一樣，只是這次是意外情況而非設計。"""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    config = make_config(db_path)

    bar = Bar(symbol="2408", ts=datetime(2026, 8, 7), open=10, high=11, low=9, close=10, volume=100)
    monkeypatch.setattr(daily_update, "detect_market_and_fetch_bars", lambda code, period: ([bar], "TWSE"))
    monkeypatch.setattr(daily_update.finmind_client, "fetch_margin_balances_for_range", lambda s, a, b: [])
    monkeypatch.setattr(
        daily_update.twse_client,
        "fetch_valuations_for_date",
        lambda d: [{"symbol": "2408", "name": "南亞科", "date": d, "pe_ratio": 10, "dividend_yield": 1, "pb_ratio": 1}],
    )

    def raise_error(symbol, start_date, end_date):
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(daily_update.finmind_client, "fetch_institutional_flows_for_range", raise_error)

    result = daily_update.add_symbol_to_watchlist(config, "2408")

    assert result["ok"] is True
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM institutional_flows WHERE symbol = '2408'").fetchone()
    assert row is None


def test_add_symbol_backfills_margin_balances_three_years_via_finmind(tmp_path, monkeypatch):
    """2026-08-08加入：融資融券跟三大法人用同一套FinMind補歷史邏輯(TaiwanStock
    MarginPurchaseShortSale同樣是start_date~end_date範圍查詢，兩個市場都涵蓋)——驗證
    傳給finmind_client的日期範圍是bars的最早跟最晚那天。"""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    config = make_config(db_path)

    bars = [
        Bar(symbol="2408", ts=datetime(2023, 8, 7), open=10, high=11, low=9, close=10, volume=100),
        Bar(symbol="2408", ts=datetime(2026, 8, 7), open=12, high=13, low=11, close=12, volume=200),
    ]
    monkeypatch.setattr(daily_update, "detect_market_and_fetch_bars", lambda code, period: (bars, "TWSE"))
    monkeypatch.setattr(daily_update.finmind_client, "fetch_institutional_flows_for_range", lambda s, a, b: [])
    monkeypatch.setattr(
        daily_update.twse_client,
        "fetch_valuations_for_date",
        lambda d: [{"symbol": "2408", "name": "南亞科", "date": d, "pe_ratio": 10, "dividend_yield": 1, "pb_ratio": 1}],
    )

    captured = {}

    def fake_margins(symbol, start_date, end_date):
        captured["args"] = (symbol, start_date, end_date)
        return [
            {
                "symbol": symbol, "date": start_date, "margin_buy": 100, "margin_sell": 50,
                "margin_balance": 5000, "short_buy": 10, "short_sell": 5, "short_balance": 200,
            }
        ]

    monkeypatch.setattr(daily_update.finmind_client, "fetch_margin_balances_for_range", fake_margins)

    result = daily_update.add_symbol_to_watchlist(config, "2408")

    assert result["ok"] is True
    assert captured["args"] == ("2408", "2023-08-07", "2026-08-07")
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM margin_balances WHERE symbol = '2408'").fetchone()
    assert row["margin_balance"] == 5000


def test_add_symbol_falls_back_to_latest_day_when_finmind_margin_fails(tmp_path, monkeypatch):
    """融資融券的FinMind回補跟三大法人是各自獨立的try/except——這個失敗不該影響三大法人
    那邊已經抓到的資料，也不該讓新增股票整個失敗。"""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    config = make_config(db_path)

    bar = Bar(symbol="2408", ts=datetime(2026, 8, 7), open=10, high=11, low=9, close=10, volume=100)
    monkeypatch.setattr(daily_update, "detect_market_and_fetch_bars", lambda code, period: ([bar], "TWSE"))
    monkeypatch.setattr(
        daily_update.finmind_client,
        "fetch_institutional_flows_for_range",
        lambda s, a, b: [{"symbol": s, "date": "2026-08-07", "foreign_net": 100, "trust_net": 50, "dealer_net": 0, "total_net": 150}],
    )
    monkeypatch.setattr(
        daily_update.twse_client,
        "fetch_valuations_for_date",
        lambda d: [{"symbol": "2408", "name": "南亞科", "date": d, "pe_ratio": 10, "dividend_yield": 1, "pb_ratio": 1}],
    )

    def raise_error(symbol, start_date, end_date):
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(daily_update.finmind_client, "fetch_margin_balances_for_range", raise_error)

    result = daily_update.add_symbol_to_watchlist(config, "2408")

    assert result["ok"] is True
    with connect(db_path) as conn:
        assert conn.execute("SELECT * FROM margin_balances WHERE symbol = '2408'").fetchone() is None
        flow_row = conn.execute("SELECT * FROM institutional_flows WHERE symbol = '2408'").fetchone()
    assert flow_row["foreign_net"] == 100


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
    monkeypatch.setattr(daily_update.finmind_client, "fetch_margin_balances_for_range", lambda s, a, b: [])
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
    monkeypatch.setattr(daily_update.finmind_client, "fetch_margin_balances_for_range", lambda s, a, b: [])
    monkeypatch.setattr(daily_update.finmind_client, "fetch_valuations_for_range", lambda s, a, b: [])
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
    monkeypatch.setattr(daily_update.finmind_client, "fetch_margin_balances_for_range", lambda s, a, b: [])
    monkeypatch.setattr(daily_update.finmind_client, "fetch_valuations_for_range", lambda s, a, b: [])
    monkeypatch.setattr(daily_update.tpex_client, "fetch_ex_dividend_schedule", lambda: [])

    calls = []
    monkeypatch.setattr(
        daily_update.finmind_client, "fetch_institutional_flows_for_range", lambda *a: calls.append(a)
    )

    daily_update._refresh_market_data_tpex(config, {"8299"})

    assert calls == [], "已經抓過今天的資料，不該再打一次FinMind"


def test_refresh_market_data_tpex_backfills_margin_and_valuation_via_finmind(tmp_path, monkeypatch):
    """2026-08-13改用FinMind：融資融券/估值也改成跟三大法人同一套「上次抓到的日期+1」~
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
        "fetch_margin_balances_for_range",
        lambda s, a, b: [
            {"symbol": s, "date": b, "margin_buy": 1, "margin_sell": 1, "margin_balance": 100, "short_buy": 1, "short_sell": 1, "short_balance": 10}
        ],
    )
    monkeypatch.setattr(
        daily_update.finmind_client,
        "fetch_valuations_for_range",
        lambda s, a, b: [{"symbol": s, "date": b, "pe_ratio": 15.0, "dividend_yield": 2.0, "pb_ratio": 3.0}],
    )

    daily_update._refresh_market_data_tpex(config, {"8299"})

    with connect(db_path) as conn:
        margin_row = conn.execute("SELECT * FROM margin_balances WHERE symbol = '8299'").fetchone()
        valuation_row = conn.execute("SELECT * FROM valuations WHERE symbol = '8299'").fetchone()
    assert margin_row["margin_balance"] == 100
    assert valuation_row["pe_ratio"] == 15.0


def test_refresh_market_data_tpex_margin_failure_does_not_block_flows_and_valuation(tmp_path, monkeypatch):
    """三個FinMind資料源各自獨立try/except：融資融券連線失敗不該讓三大法人/估值那兩個
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

    monkeypatch.setattr(daily_update.finmind_client, "fetch_margin_balances_for_range", raise_ssl_error)
    monkeypatch.setattr(
        daily_update.finmind_client,
        "fetch_valuations_for_range",
        lambda s, a, b: [{"symbol": s, "date": b, "pe_ratio": 15.0, "dividend_yield": 2.0, "pb_ratio": 3.0}],
    )

    daily_update._refresh_market_data_tpex(config, {"8299"})

    with connect(db_path) as conn:
        flow_row = conn.execute("SELECT * FROM institutional_flows WHERE symbol = '8299'").fetchone()
        margin_row = conn.execute("SELECT * FROM margin_balances WHERE symbol = '8299'").fetchone()
        valuation_row = conn.execute("SELECT * FROM valuations WHERE symbol = '8299'").fetchone()
    assert flow_row["foreign_net"] == 100
    assert margin_row is None
    assert valuation_row["pe_ratio"] == 15.0


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
