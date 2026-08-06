import sqlite3
from contextlib import contextmanager
from datetime import datetime

import pandas as pd

from stocks.models import Bar, Direction, SignalEvent, Tier

SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    code TEXT PRIMARY KEY,
    name TEXT,
    market TEXT,
    is_watchlist INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bars_5min (
    symbol TEXT NOT NULL,
    ts TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume INTEGER,
    PRIMARY KEY (symbol, ts)
);

CREATE TABLE IF NOT EXISTS bars_daily (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume INTEGER,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS signal_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    ts TEXT NOT NULL,
    strategy TEXT NOT NULL,
    direction TEXT NOT NULL,
    price REAL NOT NULL,
    detail TEXT,
    tier TEXT NOT NULL,
    UNIQUE (symbol, strategy, direction, ts)
);

CREATE TABLE IF NOT EXISTS price_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    target_price REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connectivity_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event_type TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS institutional_flows (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    foreign_net INTEGER,
    trust_net INTEGER,
    dealer_net INTEGER,
    total_net INTEGER,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS margin_balances (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    margin_buy INTEGER,
    margin_sell INTEGER,
    margin_balance INTEGER,
    short_buy INTEGER,
    short_sell INTEGER,
    short_balance INTEGER,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS valuations (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    pe_ratio REAL,
    dividend_yield REAL,
    pb_ratio REAL,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS ex_dividend_schedule (
    symbol TEXT NOT NULL,
    ex_date TEXT NOT NULL,
    cash_dividend REAL,
    stock_dividend_ratio TEXT,
    detail TEXT,
    PRIMARY KEY (symbol, ex_date)
);

CREATE TABLE IF NOT EXISTS market_data_sync_log (
    date TEXT PRIMARY KEY,
    checked_at TEXT NOT NULL
);
"""


@contextmanager
def connect(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS doesn't add columns to a table that already existed
    before that column was introduced -- patch those in for DBs created by an older schema."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(symbols)")}
    if "sort_order" not in columns:
        conn.execute("ALTER TABLE symbols ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")


def insert_bars_daily(conn: sqlite3.Connection, bars: list[Bar]) -> None:
    conn.executemany(
        """INSERT OR REPLACE INTO bars_daily (symbol, date, open, high, low, close, volume)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [(b.symbol, b.ts.strftime("%Y-%m-%d"), b.open, b.high, b.low, b.close, b.volume) for b in bars],
    )


def insert_bars_5min(conn: sqlite3.Connection, bars: list[Bar]) -> None:
    conn.executemany(
        """INSERT OR REPLACE INTO bars_5min (symbol, ts, open, high, low, close, volume)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [(b.symbol, b.ts.isoformat(), b.open, b.high, b.low, b.close, b.volume) for b in bars],
    )


def fetch_bars_daily(conn: sqlite3.Connection, symbol: str, limit: int | None = None):
    query = "SELECT * FROM bars_daily WHERE symbol = ? ORDER BY date ASC"
    rows = conn.execute(query, (symbol,)).fetchall()
    if limit:
        rows = rows[-limit:]
    return rows


def fetch_bars_5min(conn: sqlite3.Connection, symbol: str, limit: int | None = None):
    query = "SELECT * FROM bars_5min WHERE symbol = ? ORDER BY ts ASC"
    rows = conn.execute(query, (symbol,)).fetchall()
    if limit:
        rows = rows[-limit:]
    return rows


def insert_signal_events(conn: sqlite3.Connection, events: list[SignalEvent]) -> list[SignalEvent]:
    """Insert events, ignoring duplicates (same symbol/strategy/direction/ts).
    Returns only the events that were actually newly inserted."""
    newly_inserted = []
    for e in events:
        cur = conn.execute(
            """INSERT OR IGNORE INTO signal_events (symbol, ts, strategy, direction, price, detail, tier)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (e.symbol, e.ts.isoformat(), e.strategy, e.direction.value, e.price, e.detail, e.tier.value),
        )
        if cur.rowcount > 0:
            newly_inserted.append(e)
    return newly_inserted


def fetch_signal_events(conn: sqlite3.Connection, symbol: str | None = None, limit: int = 200):
    if symbol:
        query = "SELECT * FROM signal_events WHERE symbol = ? ORDER BY ts DESC LIMIT ?"
        return conn.execute(query, (symbol, limit)).fetchall()
    query = "SELECT * FROM signal_events ORDER BY ts DESC LIMIT ?"
    return conn.execute(query, (limit,)).fetchall()


def upsert_symbol(conn: sqlite3.Connection, code: str, name: str = "", market: str = "", is_watchlist: bool = False) -> None:
    """Callers that don't know the name yet (e.g. a price-only refresh) pass name="" --
    that must never blank out a name we already captured from a previous chips-data fetch."""
    conn.execute(
        """INSERT INTO symbols (code, name, market, is_watchlist) VALUES (?, ?, ?, ?)
           ON CONFLICT(code) DO UPDATE SET
               name=CASE WHEN excluded.name != '' THEN excluded.name ELSE symbols.name END,
               market=excluded.market, is_watchlist=excluded.is_watchlist""",
        (code, name, market, int(is_watchlist)),
    )


def fetch_watchlist(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM symbols WHERE is_watchlist = 1 ORDER BY sort_order ASC, code ASC").fetchall()


def add_to_watchlist(conn: sqlite3.Connection, code: str, name: str = "", market: str = "TWSE") -> None:
    """Add a symbol to the watchlist, placing it last in sort order. Re-adding a
    previously removed symbol puts it back at the end rather than its old position."""
    max_order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) AS m FROM symbols").fetchone()["m"]
    conn.execute(
        """INSERT INTO symbols (code, name, market, is_watchlist, sort_order) VALUES (?, ?, ?, 1, ?)
           ON CONFLICT(code) DO UPDATE SET is_watchlist=1, sort_order=excluded.sort_order,
               name=CASE WHEN excluded.name != '' THEN excluded.name ELSE symbols.name END""",
        (code, name, market, max_order + 1),
    )


def remove_from_watchlist(conn: sqlite3.Connection, code: str) -> None:
    """Soft-remove: keeps history/name, just stops it showing up as an active watchlist symbol."""
    conn.execute("UPDATE symbols SET is_watchlist = 0 WHERE code = ?", (code,))


def set_watchlist_order(conn: sqlite3.Connection, code: str, sort_order: int) -> None:
    conn.execute("UPDATE symbols SET sort_order = ? WHERE code = ?", (sort_order, code))


def insert_connectivity_event(conn: sqlite3.Connection, event_type: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO connectivity_events (ts, event_type, detail) VALUES (?, ?, ?)",
        (datetime.now().isoformat(), event_type, detail),
    )


def bars_to_dataframe(rows, ts_field: str) -> pd.DataFrame:
    """Convert sqlite3.Row results from bars_daily/bars_5min into an OHLCV DataFrame
    indexed by timestamp, the shape every Strategy.evaluate() expects."""
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    index = pd.to_datetime([r[ts_field] for r in rows])
    return pd.DataFrame(
        {
            "open": [r["open"] for r in rows],
            "high": [r["high"] for r in rows],
            "low": [r["low"] for r in rows],
            "close": [r["close"] for r in rows],
            "volume": [r["volume"] for r in rows],
        },
        index=index,
    )


def fetch_trading_dates(conn: sqlite3.Connection) -> list[str]:
    """Distinct trading dates already in bars_daily (all symbols share the same TWSE
    calendar), used to backfill institutional/margin/valuation data over the same range
    without guessing at holidays."""
    rows = conn.execute("SELECT DISTINCT date FROM bars_daily ORDER BY date ASC").fetchall()
    return [r["date"] for r in rows]


def fetch_synced_market_dates(conn: sqlite3.Connection) -> set[str]:
    """Dates already attempted for institutional/margin/valuation data -- tracked
    separately from whether TWSE actually had rows, so a date TWSE genuinely has no
    data for (a data gap, not a holiday) doesn't get retried forever."""
    rows = conn.execute("SELECT date FROM market_data_sync_log").fetchall()
    return {r["date"] for r in rows}


def mark_market_data_synced(conn: sqlite3.Connection, dates: list[str]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO market_data_sync_log (date, checked_at) VALUES (?, ?)",
        [(d, datetime.now().isoformat()) for d in dates],
    )


def insert_institutional_flows(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.executemany(
        """INSERT OR REPLACE INTO institutional_flows (symbol, date, foreign_net, trust_net, dealer_net, total_net)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [(r["symbol"], r["date"], r["foreign_net"], r["trust_net"], r["dealer_net"], r["total_net"]) for r in rows],
    )


def fetch_institutional_flows(conn: sqlite3.Connection, symbol: str):
    return conn.execute(
        "SELECT * FROM institutional_flows WHERE symbol = ? ORDER BY date ASC", (symbol,)
    ).fetchall()


def insert_margin_balances(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.executemany(
        """INSERT OR REPLACE INTO margin_balances
           (symbol, date, margin_buy, margin_sell, margin_balance, short_buy, short_sell, short_balance)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                r["symbol"], r["date"], r["margin_buy"], r["margin_sell"], r["margin_balance"],
                r["short_buy"], r["short_sell"], r["short_balance"],
            )
            for r in rows
        ],
    )


def fetch_margin_balances(conn: sqlite3.Connection, symbol: str):
    return conn.execute(
        "SELECT * FROM margin_balances WHERE symbol = ? ORDER BY date ASC", (symbol,)
    ).fetchall()


def insert_valuations(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.executemany(
        """INSERT OR REPLACE INTO valuations (symbol, date, pe_ratio, dividend_yield, pb_ratio)
           VALUES (?, ?, ?, ?, ?)""",
        [(r["symbol"], r["date"], r["pe_ratio"], r["dividend_yield"], r["pb_ratio"]) for r in rows],
    )


def fetch_valuations(conn: sqlite3.Connection, symbol: str):
    return conn.execute(
        "SELECT * FROM valuations WHERE symbol = ? ORDER BY date ASC", (symbol,)
    ).fetchall()


def insert_ex_dividend_schedule(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.executemany(
        """INSERT OR REPLACE INTO ex_dividend_schedule (symbol, ex_date, cash_dividend, stock_dividend_ratio, detail)
           VALUES (?, ?, ?, ?, ?)""",
        [(r["symbol"], r["ex_date"], r["cash_dividend"], r["stock_dividend_ratio"], r["detail"]) for r in rows],
    )


def fetch_ex_dividend_schedule(conn: sqlite3.Connection, symbol: str):
    return conn.execute(
        "SELECT * FROM ex_dividend_schedule WHERE symbol = ? ORDER BY ex_date ASC", (symbol,)
    ).fetchall()
