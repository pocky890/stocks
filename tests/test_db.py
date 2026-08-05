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
