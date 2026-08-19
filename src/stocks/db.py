import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from stocks.models import Bar, Direction, SignalEvent, Tier

SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    code TEXT PRIMARY KEY,
    name TEXT,
    market TEXT,
    is_watchlist INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 0,
    disabled_strategies TEXT,
    groups TEXT,
    industry_code TEXT
);

CREATE TABLE IF NOT EXISTS industry_closes (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    industry_code TEXT NOT NULL,
    close REAL,
    PRIMARY KEY (symbol, date)
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

CREATE TABLE IF NOT EXISTS valuations (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    pe_ratio REAL,
    dividend_yield REAL,
    pb_ratio REAL,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS monthly_revenue (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    revenue_year INTEGER NOT NULL,
    revenue_month INTEGER NOT NULL,
    revenue REAL,
    PRIMARY KEY (symbol, revenue_year, revenue_month)
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

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
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
    if "disabled_strategies" not in columns:
        conn.execute("ALTER TABLE symbols ADD COLUMN disabled_strategies TEXT")
    if "groups" not in columns:
        conn.execute("ALTER TABLE symbols ADD COLUMN groups TEXT")
    if "industry_code" not in columns:
        conn.execute("ALTER TABLE symbols ADD COLUMN industry_code TEXT")


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


def fetch_bars_5min_today(conn: sqlite3.Connection, symbol: str):
    """給run_live.py組「今天還在累積中的部分K棒」用——嚴格只看日曆上的今天，非交易日
    (週末/國定假日)run_live.py本來就不會執行，這裡本來就該是空的，不需要退回上一個
    交易日(那樣反而會讓「今天的部分K棒」誤把上一個交易日的資料當成今天)。

    dashboard顯示用的「今日走勢」不要用這個函式，改用fetch_bars_5min_latest_day
    (2026-08-17新增)——那邊非交易日該顯示上一個交易日的走勢，語意不一樣。"""
    today = datetime.now().strftime("%Y-%m-%d")
    return conn.execute(
        "SELECT * FROM bars_5min WHERE symbol = ? AND ts >= ? ORDER BY ts ASC",
        (symbol, today),
    ).fetchall()


def fetch_bars_5min_latest_day(conn: sqlite3.Connection, symbol: str):
    """給dashboard「今日走勢」迷你K線圖/現價計算用——2026-08-17修正：原本共用
    fetch_bars_5min_today(嚴格篩選日曆上的今天)，非交易日(週末/國定假日)當天沒有
    run_live.py收集的資料，會整個顯示空白、現價/漲跌也因此少了「今天」這個基準，錯誤
    退化成跟前一交易日同一天比較(漲跌恆為0)。改成抓「這支股票bars_5min裡實際最新的
    那一天」，非交易日自然會退回顯示最近一個交易日(通常是上一個週五)的走勢，不是
    憑空消失；交易日當中run_live.py正常運作時，「最新的那一天」自然就是今天，效果
    跟原本一樣。run_live.py沒跑起來過的股票這裡自然是空的。"""
    latest_date_row = conn.execute(
        "SELECT MAX(date(ts)) AS d FROM bars_5min WHERE symbol = ?", (symbol,)
    ).fetchone()
    if latest_date_row is None or latest_date_row["d"] is None:
        return []
    latest_date = latest_date_row["d"]
    return conn.execute(
        "SELECT * FROM bars_5min WHERE symbol = ? AND date(ts) = ? ORDER BY ts ASC",
        (symbol, latest_date),
    ).fetchall()


def insert_signal_events(conn: sqlite3.Connection, events: list[SignalEvent]) -> list[SignalEvent]:
    """Insert events, ignoring duplicates (same symbol/strategy/direction/ts) and consecutive
    same-direction repeats for the same (symbol, strategy)——2026-08-18使用者反映「訊號紀錄」
    裡同一支股票同一支策略的同一個方向(例如golden_cross的buy)會每5分鐘重複出現一整個下午：
    run_live.py對NOTIFIABLE_STRATEGIES的日線再評估(daily_bars_with_today)是對整段歷史重新
    跑一次edge-triggered策略、把ts換成當下時間(見run_live.py同一段的說明)，只要盤中分數在
    門檻附近反覆達標，同一個還沒解除的方向就會被當成「新」事件一直插入——不是真的每次都是
    新訊號，是重複觸發同一個還沒改變的狀態。這裡改成如果這個(symbol, strategy)最新一筆
    紀錄的方向跟這次要插入的相同，就跳過不插入，只有方向真的翻轉(BUY→SELL或反之)才算新
    事件。同一次呼叫裡按順序處理，前面剛插入的事件在同一個connection上就看得到(sqlite同
    connection的read-your-writes)，所以批次回補(一次可能有很多筆同symbol/strategy事件)
    也能正確逐筆比較。
    Returns only the events that were actually newly inserted."""
    newly_inserted = []
    for e in events:
        last = conn.execute(
            "SELECT direction FROM signal_events WHERE symbol = ? AND strategy = ? ORDER BY ts DESC LIMIT 1",
            (e.symbol, e.strategy),
        ).fetchone()
        if last is not None and last["direction"] == e.direction.value:
            continue
        cur = conn.execute(
            """INSERT OR IGNORE INTO signal_events (symbol, ts, strategy, direction, price, detail, tier)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (e.symbol, e.ts.isoformat(), e.strategy, e.direction.value, e.price, e.detail, e.tier.value),
        )
        if cur.rowcount > 0:
            newly_inserted.append(e)
    return newly_inserted


def fetch_signal_events(
    conn: sqlite3.Connection,
    symbol: str | None = None,
    strategy: str | None = None,
    limit: int = 200,
    symbols: list[str] | None = None,
):
    """symbols(可選)把結果限制在這份清單裡——dashboard的「訊號歷史紀錄」只想看觀察清單，
    不想看run_batch.py全市場掃描(~2000檔非觀察清單股票，tier=batch)留下的紀錄，2026-08-14
    使用者確認只留觀察清單。要限制在查詢裡做(不是查出來再用Python篩)，不然LIMIT會先
    被全市場那些更新的紀錄佔滿，篩完可能剩沒幾筆。"""
    conditions = []
    params: list = []
    if symbol:
        conditions.append("symbol = ?")
        params.append(symbol)
    if symbols:
        conditions.append(f"symbol IN ({','.join('?' for _ in symbols)})")
        params.extend(symbols)
    if strategy:
        conditions.append("strategy = ?")
        params.append(strategy)

    query = "SELECT * FROM signal_events"
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    return conn.execute(query, params).fetchall()


def find_last_entry_event(conn: sqlite3.Connection, symbol: str, strategy: str):
    """給出場通知用：查(symbol, strategy)最近2筆事件，倒數第一筆通常就是剛寫入signal_
    events的這次SELL本身，回傳倒數第二筆——如果是BUY就是這次出場對應的進場紀錄(日期/
    價位)，可以拿來算報酬率。insert_signal_events()保證同一個(symbol, strategy)只有
    方向真的翻轉才會新增紀錄，所以倒數第二筆理論上一定跟這次方向相反。找不到(例如這是
    這個(symbol, strategy)有紀錄以來的第一筆事件，沒有更早的進場可對照)或倒數第二筆
    不是BUY，就回傳None，通知那邊優雅降級不顯示這段。"""
    rows = fetch_signal_events(conn, symbol=symbol, strategy=strategy, limit=2)
    if len(rows) < 2 or rows[1]["direction"] != Direction.BUY.value:
        return None
    return rows[1]


SIGNAL_EVENTS_RETENTION_DAYS = 90  # 2026-08-14使用者要求「訊號歷史紀錄」只留3個月，
# 不然signal_events會一直累積(尤其是每5分鐘一次的指標訊號)。build_paper_trades/
# build_strategy_recommendations/_compute_track_records都是直接從bars_daily重新評估策略，
# 不讀signal_events，所以刪掉舊紀錄不影響任何分析功能，純粹只是「訊號歷史紀錄」這個
# 顯示/稽核用的log表。


def prune_signal_events(conn: sqlite3.Connection, retention_days: int = SIGNAL_EVENTS_RETENTION_DAYS) -> int:
    """刪掉超過retention_days天的舊訊號紀錄，回傳刪除的筆數。門檻用Python的datetime.now()
    (本機時區)算，不能用SQLite的datetime('now', ...)——那是UTC時間，跟寫入ts欄位時用的
    datetime.now()差了時區的時數，兩個時鐘對不上時，字串比較在特定時間點會失準(這裡踩過
    一次真的bug：ts用'T'分隔日期時間、SQLite的datetime()輸出用空白分隔，日期部分剛好落在
    同一天時，比較退化成單純比較分隔字元的ASCII值，'T'>' '導致該刪的沒刪掉)。"""
    threshold = (datetime.now() - timedelta(days=retention_days)).isoformat()
    cur = conn.execute("DELETE FROM signal_events WHERE ts < ?", (threshold,))
    return cur.rowcount


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


def get_disabled_strategies(conn: sqlite3.Connection, code: str) -> list[str]:
    """回傳某支股票目前被排除的策略清單(scripts/recompute_strategy_selection.py寫入)——
    排除的意思是run_live.py/run_batch.py不會再幫這支股票評估/通知這些策略，但dashboard的
    回測/建議買進分析頁面不受影響，還是會照樣顯示所有策略供參考。沒有紀錄(從沒跑過
    recompute，或這支股票所有策略都夠好)就回傳空清單。"""
    row = conn.execute("SELECT disabled_strategies FROM symbols WHERE code = ?", (code,)).fetchone()
    if row is None or not row["disabled_strategies"]:
        return []
    return json.loads(row["disabled_strategies"])


def set_disabled_strategies(conn: sqlite3.Connection, code: str, strategies: list[str]) -> None:
    conn.execute("UPDATE symbols SET disabled_strategies = ? WHERE code = ?", (json.dumps(strategies), code))


def get_symbol_groups(conn: sqlite3.Connection, code: str) -> list[str]:
    """使用者自訂的觀察清單分組(標籤式，一支股票可以同時屬於多個群組)——2026-08-17
    新增，純粹是dashboard顯示用的分類，不影響任何策略評估/通知邏輯。沒有設定過就回傳
    空清單(還沒分類，只會出現在「全部」)。"""
    row = conn.execute("SELECT groups FROM symbols WHERE code = ?", (code,)).fetchone()
    if row is None or not row["groups"]:
        return []
    return json.loads(row["groups"])


def set_symbol_groups(conn: sqlite3.Connection, code: str, groups: list[str]) -> None:
    conn.execute("UPDATE symbols SET groups = ? WHERE code = ?", (json.dumps(groups), code))


def upsert_industry_universe(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """批次寫入「全市場同產業股票」的代號/名稱/市場/產業代碼——這些大多不是觀察清單
    股票，只是為了算circuit_breaker.py的產業寬度需要知道「這個產業全市場還有哪些股票」。
    刻意不動is_watchlist(不在SET子句裡，ON CONFLICT時會維持原值)：如果某支股票剛好
    同時是觀察清單成員(例如2330台積電)，這個函式不會把它的is_watchlist=1覆蓋掉；
    也不會用空字串蓋掉已經有的名稱，跟upsert_symbol同樣的保守慣例。"""
    conn.executemany(
        """INSERT INTO symbols (code, name, market, industry_code) VALUES (?, ?, ?, ?)
           ON CONFLICT(code) DO UPDATE SET
               industry_code = excluded.industry_code,
               name = CASE WHEN excluded.name != '' THEN excluded.name ELSE symbols.name END,
               market = excluded.market""",
        [(r["code"], r.get("name", ""), r["market"], r["industry_code"]) for r in rows],
    )


def get_industry_code(conn: sqlite3.Connection, code: str) -> str | None:
    """證交所/櫃買中心官方產業分類代碼(twse_client/tpex_client的公司名錄API撈的，
    例如"24"=半導體業)，用來算「全市場同產業寬度」斷路器——不是使用者自訂的groups
    分類標籤，那個是人為的觀察清單分組，跟這裡的官方產業別是兩回事。沒有值代表還沒
    分類過(通常是還沒跑過populate_industry_codes.py，或這支股票是這之後才新增的)。"""
    row = conn.execute("SELECT industry_code FROM symbols WHERE code = ?", (code,)).fetchone()
    return row["industry_code"] if row else None


def set_industry_code(conn: sqlite3.Connection, code: str, industry_code: str | None) -> None:
    conn.execute("UPDATE symbols SET industry_code = ? WHERE code = ?", (industry_code, code))


def fetch_all_industry_codes(conn: sqlite3.Connection) -> dict[str, str]:
    """回傳{code: industry_code}，只含有分類過的(industry_code不是NULL)。"""
    rows = conn.execute("SELECT code, industry_code FROM symbols WHERE industry_code IS NOT NULL").fetchall()
    return {r["code"]: r["industry_code"] for r in rows}


def insert_industry_closes(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """rows是{"symbol", "date", "industry_code", "close"}的list——全市場(不限觀察清單)
    跟觀察清單同產業的股票，收盤價存這裡供斷路器算「這個產業有幾成股票跌破自己20日均線」
    用，不是給策略評估用的完整OHLCV，只留算均線寬度需要的收盤價，比bars_daily輕量。"""
    conn.executemany(
        """INSERT OR REPLACE INTO industry_closes (symbol, date, industry_code, close)
           VALUES (?, ?, ?, ?)""",
        [(r["symbol"], r["date"], r["industry_code"], r["close"]) for r in rows],
    )


def fetch_industry_closes(conn: sqlite3.Connection, industry_code: str, since_date: str | None = None):
    """回傳某個產業代碼底下全市場所有股票的收盤價紀錄，供circuit_breaker.py算寬度用。
    since_date(可選)只拿這天之後的資料，斷路器只需要算MA20+遲滯緩衝的觀察窗口，
    不需要整個retention期間全部資料都掃過一次。"""
    if since_date:
        return conn.execute(
            "SELECT * FROM industry_closes WHERE industry_code = ? AND date >= ? ORDER BY date ASC",
            (industry_code, since_date),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM industry_closes WHERE industry_code = ? ORDER BY date ASC", (industry_code,)
    ).fetchall()


INDUSTRY_CLOSES_RETENTION_DAYS = 180  # 算MA20寬度+遲滯緩衝只需要近期資料，跟
# signal_events的90天retention同樣道理，不用整個全市場歷史都留著。


def prune_industry_closes(conn: sqlite3.Connection, retention_days: int = INDUSTRY_CLOSES_RETENTION_DAYS) -> int:
    cur = conn.execute("DELETE FROM industry_closes WHERE date < date('now', ?)", (f"-{retention_days} days",))
    return cur.rowcount


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


def watchlist_sync_path(db_path: str) -> Path:
    """觀察清單/群組跨機器同步用的檔案路徑，跟db_path放同一個目錄——2026-08-17使用者
    有兩台電腦各自跑這個專案，歷史股價/籌碼資料量太大不適合整個用git同步，只把「使用者
    自己設定的清單長相」(代號/名稱/市場/排序/群組)獨立寫成一個小檔案，這個檔案沒有被
    .gitignore排除，使用者自己git add/commit/push就能同步到另一台機器。"""
    return Path(db_path).parent / "watchlist_shared.json"


def export_watchlist_snapshot(conn: sqlite3.Connection, path: Path | str) -> None:
    """把目前的觀察清單(代號/名稱/市場/排序/群組)寫成JSON檔案。刻意不包含
    disabled_strategies——那是根據本地歷史資料算出來的，兩台機器歷史資料進度不一定
    一樣，不該被另一台機器的匯出蓋掉，讓各自的scripts/recompute_strategy_selection.py
    自己重新判斷。呼叫端要在每次會改動觀察清單/群組的操作後呼叫這個函式(新增/移除/
    排序/改群組)，讓匯出檔案隨時反映最新狀態。"""
    rows = fetch_watchlist(conn)
    snapshot = [
        {
            "code": r["code"],
            "name": r["name"] or "",
            "market": r["market"] or "",
            "sort_order": r["sort_order"],
            "groups": json.loads(r["groups"]) if r["groups"] else [],
        }
        for r in rows
    ]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)


def import_watchlist_snapshot(conn: sqlite3.Connection, path: Path | str) -> bool:
    """把export_watchlist_snapshot寫出來的檔案套用回本地資料庫——檔案裡沒有的代號視為
    另一台機器已經移除，本地也跟著移除(is_watchlist=0，軟刪除，歷史資料還留著)；檔案裡
    有的upsert代號/名稱/市場/排序/群組。回傳True代表本地資料庫真的因此有改動，呼叫端
    可以用這個判斷要不要提示使用者/重新整理。同樣刻意不動disabled_strategies跟任何
    歷史資料表。檔案不存在(例如這台機器從來沒匯出過、或還沒git pull過)就直接跳過，
    不當成錯誤。"""
    path = Path(path)
    if not path.exists():
        return False
    with open(path, "r", encoding="utf-8") as f:
        snapshot = json.load(f)

    changed = False
    snapshot_codes = {row["code"] for row in snapshot}
    current = {r["code"]: r for r in fetch_watchlist(conn)}

    for row in snapshot:
        code = row["code"]
        groups_json = json.dumps(row.get("groups", []))
        existing = current.get(code)
        if (
            existing is None
            or existing["name"] != row["name"]
            or existing["market"] != row["market"]
            or existing["sort_order"] != row["sort_order"]
            or (existing["groups"] or "[]") != groups_json
        ):
            conn.execute(
                """INSERT INTO symbols (code, name, market, is_watchlist, sort_order, groups)
                   VALUES (?, ?, ?, 1, ?, ?)
                   ON CONFLICT(code) DO UPDATE SET
                       name=excluded.name, market=excluded.market, is_watchlist=1,
                       sort_order=excluded.sort_order, groups=excluded.groups""",
                (code, row["name"], row["market"], row["sort_order"], groups_json),
            )
            changed = True

    for code in current:
        if code not in snapshot_codes:
            conn.execute("UPDATE symbols SET is_watchlist = 0 WHERE code = ?", (code,))
            changed = True

    return changed


def remove_from_watchlist(conn: sqlite3.Connection, code: str) -> None:
    """Soft-remove: keeps history/name, just stops it showing up as an active watchlist symbol."""
    conn.execute("UPDATE symbols SET is_watchlist = 0 WHERE code = ?", (code,))


def set_watchlist_order(conn: sqlite3.Connection, code: str, sort_order: int) -> None:
    conn.execute("UPDATE symbols SET sort_order = ? WHERE code = ?", (sort_order, code))


def move_watchlist_symbol(conn: sqlite3.Connection, code: str, direction: int, visible_codes: set[str] | None = None) -> None:
    """direction: -1 to move up, +1 to move down. visible_codes限制「上一個/下一個鄰居」
    只能在這個集合裡找(通常是dashboard目前篩選的群組子集合)，不給的話就用整個觀察清單——
    2026-08-15使用者反映▲▼看起來壞掉：dashboard切換群組後畫面只顯示該群組的股票，但
    這裡原本直接抓「整個觀察清單」的鄰居，如果緊鄰的下一筆剛好屬於別的群組(畫面上根本
    沒顯示)，交換的對象是使用者看不到的股票，畫面上完全看不出變化，看起來像沒反應。

    Renumbers every watchlist symbol's sort_order sequentially (0..n-1) based on current
    display order and swaps the target with its (visible_codes內的)鄰居 -- this also
    self-heals any duplicate/untidy sort_order values left over from before the reorder UI
    existed, instead of requiring a separate migration。"""
    codes = [r["code"] for r in fetch_watchlist(conn)]
    visible = [c for c in codes if visible_codes is None or c in visible_codes]
    idx_in_visible = visible.index(code)
    new_idx_in_visible = idx_in_visible + direction
    if new_idx_in_visible < 0 or new_idx_in_visible >= len(visible):
        return
    neighbor = visible[new_idx_in_visible]

    idx = codes.index(code)
    neighbor_idx = codes.index(neighbor)
    codes[idx], codes[neighbor_idx] = codes[neighbor_idx], codes[idx]
    for position, c in enumerate(codes):
        set_watchlist_order(conn, c, position)


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


def bars_list_to_dataframe(bars: list[Bar]) -> pd.DataFrame:
    """跟bars_to_dataframe同樣的欄位形狀，但輸入是list[Bar]物件（例如shioaji_client現場抓
    的今日分K），不是sqlite3.Row查詢結果。"""
    if not bars:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    index = pd.to_datetime([b.ts for b in bars])
    return pd.DataFrame(
        {
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        },
        index=index,
    )


def attach_institutional_flows(bars_df: pd.DataFrame, institutional_rows) -> pd.DataFrame:
    """Left-joins foreign_net/trust_net (from institutional_flows rows, e.g. from
    fetch_institutional_flows()) onto an OHLCV bars DataFrame by date, so strategies
    like institutional_streak can read them off the same DataFrame every other
    strategy already gets. Dates bars_df has but institutional_rows doesn't get NaN,
    which InstitutionalStreakStrategy treats as neutral (breaks any streak)."""
    if not institutional_rows:
        return bars_df
    inst_df = pd.DataFrame(
        {
            "foreign_net": [r["foreign_net"] for r in institutional_rows],
            "trust_net": [r["trust_net"] for r in institutional_rows],
        },
        index=pd.to_datetime([r["date"] for r in institutional_rows]),
    )
    return bars_df.join(inst_df, how="left")


def attach_monthly_revenue_growth(bars_df: pd.DataFrame, revenue_rows, report_lag_days: int = 10) -> pd.DataFrame:
    """把月營收年增率(revenue_yoy_growth，百分比)接到bars_df上，供基本面濾網研究用。
    跟attach_institutional_flows不同的是不能直接join——月營收一個月才一筆，中間的
    交易日要往前forward-fill沿用「最近一次已公布的年增率」，而且要避免look-ahead：
    FinMind的date欄位是「營收所屬月份的下個月1號」，不是公司實際公告日(公司法規公告
    期限是次月10日前)，這裡用date+report_lag_days(預設10天，抓公告期限的上緣，寧可
    晚一點才算「已知」也不要提早)當作這筆年增率真正「可以被拿來用」的日期，這個日期
    之前的交易日這筆資料一律當作還不知道(NaN)。

    年增率=（今年這個月營收-去年同月營收）/去年同月營收，任一邊缺資料(通常是資料
    還沒回補到10年前、或是新股票剛上市不到一年)就跳過那個月，不會拿0或錯誤基期
    硬算出一個誤導的數字。沒有任何一筆算得出年增率(revenue_rows是空的、或每一筆都
    缺去年同期基期)時，整欄位是NaN，呼叫端(策略/濾網)要自己決定NaN要不要當作
    「未知就不擋」還是「未知就保守擋」。"""
    if not revenue_rows:
        bars_df = bars_df.copy()
        bars_df["revenue_yoy_growth"] = float("nan")
        return bars_df

    by_period = {(r["revenue_year"], r["revenue_month"]): r["revenue"] for r in revenue_rows}

    records = []
    for r in revenue_rows:
        year, month, revenue = r["revenue_year"], r["revenue_month"], r["revenue"]
        prior = by_period.get((year - 1, month))
        if prior is None or not prior or revenue is None:
            continue
        yoy_growth = (revenue - prior) / prior * 100
        available_date = pd.Timestamp(r["date"]) + pd.Timedelta(days=report_lag_days)
        records.append((available_date, yoy_growth))

    bars_df = bars_df.copy()
    if not records:
        bars_df["revenue_yoy_growth"] = float("nan")
        return bars_df

    growth = pd.Series(
        [v for _, v in records], index=pd.DatetimeIndex([d for d, _ in records]), name="revenue_yoy_growth"
    ).sort_index()
    combined_index = bars_df.index.union(growth.index)
    growth_filled = growth.reindex(combined_index).ffill()
    bars_df["revenue_yoy_growth"] = growth_filled.reindex(bars_df.index)
    return bars_df


def fetch_trading_dates(conn: sqlite3.Connection) -> list[str]:
    """Distinct trading dates already in bars_daily (all symbols share the same TWSE
    calendar), used to backfill institutional/valuation data over the same range
    without guessing at holidays."""
    rows = conn.execute("SELECT DISTINCT date FROM bars_daily ORDER BY date ASC").fetchall()
    return [r["date"] for r in rows]


def fetch_synced_market_dates(conn: sqlite3.Connection) -> set[str]:
    """Dates already attempted for institutional/valuation data -- tracked
    separately from whether TWSE actually had rows, so a date TWSE genuinely has no
    data for (a data gap, not a holiday) doesn't get retried forever."""
    rows = conn.execute("SELECT date FROM market_data_sync_log").fetchall()
    return {r["date"] for r in rows}


def mark_market_data_synced(conn: sqlite3.Connection, dates: list[str]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO market_data_sync_log (date, checked_at) VALUES (?, ?)",
        [(d, datetime.now().isoformat()) for d in dates],
    )


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, value))


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


def insert_monthly_revenue(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.executemany(
        """INSERT OR REPLACE INTO monthly_revenue (symbol, date, revenue_year, revenue_month, revenue)
           VALUES (?, ?, ?, ?, ?)""",
        [(r["symbol"], r["date"], r["revenue_year"], r["revenue_month"], r["revenue"]) for r in rows],
    )


def fetch_monthly_revenue(conn: sqlite3.Connection, symbol: str):
    return conn.execute(
        "SELECT * FROM monthly_revenue WHERE symbol = ? ORDER BY revenue_year ASC, revenue_month ASC", (symbol,)
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
