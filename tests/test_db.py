from datetime import datetime

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
