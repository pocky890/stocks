from datetime import datetime, timedelta

import pandas as pd

from stocks import db
from stocks.models import Direction, SignalEvent, Tier


def make_event(strategy="ma_crossover", direction=Direction.BUY, ts=None, symbol="2330"):
    return SignalEvent(
        symbol=symbol,
        strategy=strategy,
        direction=direction,
        price=600.0,
        ts=ts or datetime(2026, 1, 5, 9, 5),
        detail="test",
        tier=Tier.REALTIME,
    )


def test_init_db_creates_schema(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"symbols", "bars_5min", "bars_daily", "signal_events", "price_alerts", "connectivity_events"} <= tables


def test_insert_signal_events_dedups_identical_events(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    event = make_event()

    with db.connect(db_path) as conn:
        first_pass = db.insert_signal_events(conn, [event])
    with db.connect(db_path) as conn:
        second_pass = db.insert_signal_events(conn, [event])

    assert len(first_pass) == 1
    assert len(second_pass) == 0, "re-inserting the identical event must not be treated as new"


def test_insert_signal_events_allows_different_strategy_same_symbol_ts(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    ts = datetime(2026, 1, 5, 9, 5)
    events = [make_event(strategy="ma_crossover", ts=ts), make_event(strategy="rsi", ts=ts)]

    with db.connect(db_path) as conn:
        inserted = db.insert_signal_events(conn, events)

    assert len(inserted) == 2, "different strategies firing at the same bar are distinct events"


def test_fetch_signal_events_returns_newest_first(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    older = make_event(ts=datetime(2026, 1, 5, 9, 5))
    newer = make_event(ts=datetime(2026, 1, 5, 9, 10))

    with db.connect(db_path) as conn:
        db.insert_signal_events(conn, [older, newer])
        rows = db.fetch_signal_events(conn, symbol="2330")

    assert rows[0]["ts"] == newer.ts.isoformat()


def test_fetch_signal_events_filters_by_strategy(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    ma_event = make_event(strategy="ma_crossover")
    formula_event = make_event(strategy="buy_formula")

    with db.connect(db_path) as conn:
        db.insert_signal_events(conn, [ma_event, formula_event])
        rows = db.fetch_signal_events(conn, strategy="buy_formula")

    assert len(rows) == 1
    assert rows[0]["strategy"] == "buy_formula"


def test_fetch_signal_events_symbols_restricts_to_that_list(tmp_path):
    # 2026-08-14使用者要求「訊號歷史紀錄」只留觀察清單——run_batch.py全市場掃描
    # (~2000檔非觀察清單股票)也會寫進signal_events，symbols參數要把這些篩掉，且要在
    # SQL查詢裡篩(不是抓出來後用Python篩)，不然LIMIT會先被全市場的紀錄佔滿
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    watchlist_event = make_event(symbol="2330")
    batch_scan_event = make_event(symbol="1101")

    with db.connect(db_path) as conn:
        db.insert_signal_events(conn, [watchlist_event, batch_scan_event])
        rows = db.fetch_signal_events(conn, symbols=["2330"])

    assert len(rows) == 1
    assert rows[0]["symbol"] == "2330"


def test_prune_signal_events_deletes_only_records_older_than_retention(tmp_path):
    # 2026-08-14使用者要求「訊號歷史紀錄」只留3個月——用相對於現在的時間戳記，不要用
    # 固定日期，不然測試跑的時間點不同，90天前的判斷就會失準
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    old_event = make_event(ts=datetime.now() - timedelta(days=91))
    recent_event = make_event(ts=datetime.now() - timedelta(days=89))

    with db.connect(db_path) as conn:
        db.insert_signal_events(conn, [old_event, recent_event])
        deleted = db.prune_signal_events(conn, retention_days=90)
        rows = db.fetch_signal_events(conn)

    assert deleted == 1
    assert len(rows) == 1
    assert rows[0]["ts"] == recent_event.ts.isoformat()


def test_upsert_symbol_without_name_does_not_blank_out_existing_name(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        db.upsert_symbol(conn, "2330", name="台積電", market="TWSE", is_watchlist=True)
        # simulates a price-only refresh that doesn't know the name (e.g. daily_update._refresh_price_data)
        db.upsert_symbol(conn, "2330", market="TWSE", is_watchlist=True)
        row = conn.execute("SELECT name FROM symbols WHERE code = '2330'").fetchone()

    assert row["name"] == "台積電", "a refresh call with no name must not overwrite the name we already have"


def test_add_to_watchlist_appends_at_end_of_sort_order(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        db.add_to_watchlist(conn, "2330", name="台積電")
        db.add_to_watchlist(conn, "2317", name="鴻海")
        codes_in_order = [r["code"] for r in db.fetch_watchlist(conn)]

    assert codes_in_order == ["2330", "2317"], "second add should land after the first, not reorder it"


def test_remove_from_watchlist_hides_but_keeps_row(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        db.add_to_watchlist(conn, "2330", name="台積電")
        db.remove_from_watchlist(conn, "2330")
        active = db.fetch_watchlist(conn)
        still_exists = conn.execute("SELECT * FROM symbols WHERE code = '2330'").fetchone()

    assert active == [], "removed symbol should not appear in the active watchlist"
    assert still_exists is not None, "removal should be soft -- history/name stays in the table"


def test_set_watchlist_order_changes_fetch_order(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        db.add_to_watchlist(conn, "2330")
        db.add_to_watchlist(conn, "2317")
        db.set_watchlist_order(conn, "2317", 0)
        db.set_watchlist_order(conn, "2330", 1)
        codes_in_order = [r["code"] for r in db.fetch_watchlist(conn)]

    assert codes_in_order == ["2317", "2330"]


def test_readding_removed_symbol_moves_to_end_not_old_position(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        db.add_to_watchlist(conn, "2330")
        db.add_to_watchlist(conn, "2317")
        db.remove_from_watchlist(conn, "2330")
        db.add_to_watchlist(conn, "2330")  # re-add
        codes_in_order = [r["code"] for r in db.fetch_watchlist(conn)]

    assert codes_in_order == ["2317", "2330"], "re-adding should place it last, not restore its old slot"


def test_move_watchlist_symbol_swaps_with_neighbor(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        db.add_to_watchlist(conn, "2330")
        db.add_to_watchlist(conn, "2317")
        db.add_to_watchlist(conn, "2454")
        db.move_watchlist_symbol(conn, "2317", direction=-1)  # move 2nd item up
        codes_in_order = [r["code"] for r in db.fetch_watchlist(conn)]

    assert codes_in_order == ["2317", "2330", "2454"]


def test_move_watchlist_symbol_at_top_edge_is_a_noop(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        db.add_to_watchlist(conn, "2330")
        db.add_to_watchlist(conn, "2317")
        db.move_watchlist_symbol(conn, "2330", direction=-1)  # already first, can't go further up
        codes_in_order = [r["code"] for r in db.fetch_watchlist(conn)]

    assert codes_in_order == ["2330", "2317"]


def test_move_watchlist_symbol_self_heals_duplicate_sort_order(tmp_path):
    """Symbols added before the reorder feature existed all share sort_order=0 (the
    schema column default). A move must still produce a clean, deterministic order."""
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        conn.execute("INSERT INTO symbols (code, is_watchlist, sort_order) VALUES ('2330', 1, 0)")
        conn.execute("INSERT INTO symbols (code, is_watchlist, sort_order) VALUES ('2317', 1, 0)")
        conn.execute("INSERT INTO symbols (code, is_watchlist, sort_order) VALUES ('2454', 1, 0)")
        # tied sort_order=0 breaks by code ASC first: 2317, 2330, 2454
        db.move_watchlist_symbol(conn, "2454", direction=-1)  # move last item up one slot
        codes_in_order = [r["code"] for r in db.fetch_watchlist(conn)]

    assert codes_in_order == ["2317", "2454", "2330"]


def test_move_watchlist_symbol_skips_neighbors_outside_visible_codes(tmp_path):
    # 2026-08-15使用者反映dashboard切換群組後▲▼看起來壞掉：整個觀察清單順序是
    # 2330,3141(別的群組),2317 三檔，畫面上目前這個群組只顯示2330跟2317(3141屬於
    # 別的群組，畫面上看不到)——在這個篩選後的畫面上對2317按▲，應該要跳過看不見的
    # 3141、直接跟2330交換，不能傻傻地跟緊鄰的全域鄰居(3141)交換，不然畫面上完全
    # 看不出任何變化。
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        db.add_to_watchlist(conn, "2330")
        db.add_to_watchlist(conn, "3141")
        db.add_to_watchlist(conn, "2317")
        db.move_watchlist_symbol(conn, "2317", direction=-1, visible_codes={"2330", "2317"})
        codes_in_order = [r["code"] for r in db.fetch_watchlist(conn)]

    assert codes_in_order == ["2317", "3141", "2330"]


def test_move_watchlist_symbol_is_noop_at_edge_of_visible_codes(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        db.add_to_watchlist(conn, "2330")
        db.add_to_watchlist(conn, "3141")
        db.add_to_watchlist(conn, "2317")
        # 2330是目前群組(只有2330/2317)裡看得到的第一筆，再往上移動應該什麼都不做，
        # 不能因為全域清單裡2330前面沒有別的股票就直接回傳，也不能誤跳到3141。
        db.move_watchlist_symbol(conn, "2330", direction=-1, visible_codes={"2330", "2317"})
        codes_in_order = [r["code"] for r in db.fetch_watchlist(conn)]

    assert codes_in_order == ["2330", "3141", "2317"]


def test_bars_list_to_dataframe_converts_bar_objects():
    from stocks.models import Bar

    bars = [
        Bar(symbol="2330", ts=datetime(2026, 1, 5, 9, 0), open=100, high=101, low=99, close=100.5, volume=10),
        Bar(symbol="2330", ts=datetime(2026, 1, 5, 9, 5), open=100.5, high=102, low=100, close=101.5, volume=20),
    ]

    df = db.bars_list_to_dataframe(bars)

    assert list(df["close"]) == [100.5, 101.5]
    assert df.index[0] == datetime(2026, 1, 5, 9, 0)


def test_bars_list_to_dataframe_handles_empty_list():
    df = db.bars_list_to_dataframe([])
    assert df.empty
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]


def test_attach_institutional_flows_joins_by_date():
    bars = pd.DataFrame(
        {"close": [10.0, 11.0, 12.0]},
        index=pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"]),
    )
    institutional_rows = [
        {"date": "2026-01-05", "foreign_net": 100, "trust_net": -50},
        {"date": "2026-01-06", "foreign_net": 200, "trust_net": 60},
    ]

    merged = db.attach_institutional_flows(bars, institutional_rows)

    assert merged.loc["2026-01-05", "foreign_net"] == 100
    assert merged.loc["2026-01-06", "trust_net"] == 60
    assert pd.isna(merged.loc["2026-01-07", "foreign_net"]), "a date with no institutional row gets NaN, not dropped"


def test_attach_institutional_flows_returns_bars_unchanged_when_empty():
    bars = pd.DataFrame({"close": [10.0]}, index=pd.to_datetime(["2026-01-05"]))
    merged = db.attach_institutional_flows(bars, [])
    assert "foreign_net" not in merged.columns


def test_fetch_bars_5min_today_excludes_earlier_days(tmp_path):
    from stocks.models import Bar

    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    from datetime import timedelta

    yesterday = (datetime.now() - timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    today_bar = Bar(symbol="2330", ts=datetime.now(), open=100, high=101, low=99, close=100, volume=10)
    yesterday_bar = Bar(symbol="2330", ts=yesterday, open=90, high=91, low=89, close=90, volume=5)

    with db.connect(db_path) as conn:
        db.insert_bars_5min(conn, [today_bar, yesterday_bar])
        rows = db.fetch_bars_5min_today(conn, "2330")

    assert len(rows) == 1
    assert rows[0]["close"] == 100


def test_fetch_bars_5min_latest_day_returns_todays_bars_when_present(tmp_path):
    from datetime import timedelta

    from stocks.models import Bar

    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    yesterday = (datetime.now() - timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    today_bar = Bar(symbol="2330", ts=datetime.now(), open=100, high=101, low=99, close=100, volume=10)
    yesterday_bar = Bar(symbol="2330", ts=yesterday, open=90, high=91, low=89, close=90, volume=5)

    with db.connect(db_path) as conn:
        db.insert_bars_5min(conn, [today_bar, yesterday_bar])
        rows = db.fetch_bars_5min_latest_day(conn, "2330")

    assert len(rows) == 1
    assert rows[0]["close"] == 100


def test_fetch_bars_5min_latest_day_falls_back_to_last_trading_day_on_non_trading_day(tmp_path):
    # 2026-08-17使用者回報：非交易日(週末/國定假日)當天run_live.py沒有新增任何
    # bars_5min，「今日走勢」不該整個空白，該顯示上一個交易日(通常是上週五)的走勢
    from datetime import timedelta

    from stocks.models import Bar

    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    two_days_ago = (datetime.now() - timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
    three_days_ago = (datetime.now() - timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)
    last_trading_bar = Bar(symbol="2330", ts=two_days_ago, open=100, high=101, low=99, close=100, volume=10)
    older_bar = Bar(symbol="2330", ts=three_days_ago, open=90, high=91, low=89, close=90, volume=5)

    with db.connect(db_path) as conn:
        db.insert_bars_5min(conn, [last_trading_bar, older_bar])
        rows = db.fetch_bars_5min_latest_day(conn, "2330")

    assert len(rows) == 1, "只該回傳最新那一天(兩天前)的資料，不含更早那一天"
    assert rows[0]["close"] == 100


def test_fetch_bars_5min_latest_day_returns_empty_when_never_run(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        assert db.fetch_bars_5min_latest_day(conn, "2330") == []


def test_get_setting_returns_none_when_missing(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        assert db.get_setting(conn, "last_data_check") is None


def test_set_setting_then_get_setting_roundtrips(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        db.set_setting(conn, "last_data_check", "2026-08-07T12:00:00")
        assert db.get_setting(conn, "last_data_check") == "2026-08-07T12:00:00"


def test_set_setting_overwrites_previous_value(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        db.set_setting(conn, "last_data_check", "2026-08-07T09:00:00")
        db.set_setting(conn, "last_data_check", "2026-08-07T19:05:00")
        assert db.get_setting(conn, "last_data_check") == "2026-08-07T19:05:00"


def test_get_disabled_strategies_returns_empty_list_when_never_set(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        db.add_to_watchlist(conn, "2330")
        assert db.get_disabled_strategies(conn, "2330") == []


def test_get_disabled_strategies_returns_empty_list_for_unknown_symbol(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        assert db.get_disabled_strategies(conn, "9999") == []


def test_set_disabled_strategies_then_get_roundtrips(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        db.add_to_watchlist(conn, "2330")
        db.set_disabled_strategies(conn, "2330", ["trend_following", "atr_breakout"])
        assert db.get_disabled_strategies(conn, "2330") == ["trend_following", "atr_breakout"]


def test_set_disabled_strategies_overwrites_previous_list(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        db.add_to_watchlist(conn, "2330")
        db.set_disabled_strategies(conn, "2330", ["trend_following"])
        db.set_disabled_strategies(conn, "2330", [])
        assert db.get_disabled_strategies(conn, "2330") == []


def test_get_symbol_groups_returns_empty_list_when_never_set(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        db.add_to_watchlist(conn, "2330")
        assert db.get_symbol_groups(conn, "2330") == []


def test_set_symbol_groups_then_get_roundtrips(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        db.add_to_watchlist(conn, "2330")
        db.set_symbol_groups(conn, "2330", ["AI供應鏈", "記憶體"])
        assert db.get_symbol_groups(conn, "2330") == ["AI供應鏈", "記憶體"]


def test_set_symbol_groups_overwrites_previous_list(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        db.add_to_watchlist(conn, "2330")
        db.set_symbol_groups(conn, "2330", ["AI供應鏈"])
        db.set_symbol_groups(conn, "2330", [])
        assert db.get_symbol_groups(conn, "2330") == []


def test_watchlist_sync_path_sits_next_to_db_file():
    assert db.watchlist_sync_path("/some/dir/stocks.db") == db.Path("/some/dir/watchlist_shared.json")


def test_export_watchlist_snapshot_writes_code_name_market_order_groups(tmp_path):
    # 2026-08-17使用者要求兩台電腦共用觀察清單/群組——匯出檔案刻意不含
    # disabled_strategies，那是根據本地歷史資料算出來的，不該被同步覆蓋。
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    snapshot_path = tmp_path / "watchlist_shared.json"

    with db.connect(db_path) as conn:
        db.add_to_watchlist(conn, "2330", name="台積電", market="TWSE")
        db.set_symbol_groups(conn, "2330", ["AI供應鏈"])
        db.export_watchlist_snapshot(conn, snapshot_path)

    import json

    with open(snapshot_path, encoding="utf-8") as f:
        snapshot = json.load(f)

    assert snapshot == [{"code": "2330", "name": "台積電", "market": "TWSE", "sort_order": 0, "groups": ["AI供應鏈"]}]


def test_import_watchlist_snapshot_returns_false_when_file_missing(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        assert db.import_watchlist_snapshot(conn, tmp_path / "does_not_exist.json") is False


def test_import_watchlist_snapshot_adds_new_symbol_from_file(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    snapshot_path = tmp_path / "watchlist_shared.json"

    with db.connect(db_path) as conn_a:
        db.add_to_watchlist(conn_a, "2330", name="台積電", market="TWSE")
        db.set_symbol_groups(conn_a, "2330", ["核心"])
        db.export_watchlist_snapshot(conn_a, snapshot_path)

    other_db_path = str(tmp_path / "other.db")
    db.init_db(other_db_path)
    with db.connect(other_db_path) as conn_b:
        changed = db.import_watchlist_snapshot(conn_b, snapshot_path)
        assert changed is True
        rows = db.fetch_watchlist(conn_b)
        assert len(rows) == 1
        assert rows[0]["code"] == "2330"
        assert rows[0]["name"] == "台積電"
        assert db.get_symbol_groups(conn_b, "2330") == ["核心"]


def test_import_watchlist_snapshot_removes_symbol_not_in_file(tmp_path):
    # 另一台機器已經把某支股票移除觀察清單，這台機器套用同步檔案後也該跟著移除
    # (軟刪除，is_watchlist=0)，不是保留舊的、造成兩邊清單越來越不一致。
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    snapshot_path = tmp_path / "watchlist_shared.json"

    with db.connect(db_path) as conn:
        db.add_to_watchlist(conn, "2330")
        db.add_to_watchlist(conn, "2454")
        db.export_watchlist_snapshot(conn, snapshot_path)  # 檔案裡有2330跟2454

    with db.connect(db_path) as conn:
        db.remove_from_watchlist(conn, "2454")
        db.export_watchlist_snapshot(conn, snapshot_path)  # 重新匯出，檔案裡只剩2330

    other_db_path = str(tmp_path / "other.db")
    db.init_db(other_db_path)
    with db.connect(other_db_path) as conn_b:
        db.add_to_watchlist(conn_b, "2330")
        db.add_to_watchlist(conn_b, "2454")  # 這台機器還沒同步過，兩支都在

        changed = db.import_watchlist_snapshot(conn_b, snapshot_path)

        assert changed is True
        codes = {r["code"] for r in db.fetch_watchlist(conn_b)}
        assert codes == {"2330"}


def test_import_watchlist_snapshot_returns_false_when_already_in_sync(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    snapshot_path = tmp_path / "watchlist_shared.json"

    with db.connect(db_path) as conn:
        db.add_to_watchlist(conn, "2330", name="台積電", market="TWSE")
        db.export_watchlist_snapshot(conn, snapshot_path)
        changed = db.import_watchlist_snapshot(conn, snapshot_path)

    assert changed is False


def test_import_watchlist_snapshot_does_not_touch_disabled_strategies(tmp_path):
    # disabled_strategies是根據本地歷史資料算出來的，兩台機器歷史資料進度不一定一樣，
    # 匯入同步檔案時不該被覆蓋/清空。
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    snapshot_path = tmp_path / "watchlist_shared.json"

    with db.connect(db_path) as conn:
        db.add_to_watchlist(conn, "2330")
        db.set_disabled_strategies(conn, "2330", ["chip_momentum"])
        db.export_watchlist_snapshot(conn, snapshot_path)
        db.import_watchlist_snapshot(conn, snapshot_path)
        assert db.get_disabled_strategies(conn, "2330") == ["chip_momentum"]
