from datetime import datetime

import pandas as pd

from stocks import db
from stocks.models import Direction, SignalEvent, Tier


def make_event(strategy="ma_crossover", direction=Direction.BUY, ts=None):
    return SignalEvent(
        symbol="2330",
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
