"""供dashboard在開啟時做一次「有新資料就抓，沒有就跳過」的檢查，
重用跟scripts/fetch_historical.py、scripts/fetch_market_data.py一樣的底層client/db函式，
只是包成一個安靜、不印進度的版本，適合在網頁載入時跑。

上市(TWSE)跟上櫃(TPEx)的籌碼資料走不同邏輯：三大法人買賣超兩邊都改用FinMind(可以指定
日期範圍查詢，2026-08-07查證確認)，TWSE用sync log追蹤還缺哪些日期backfill，TPEx用「上次
抓到的日期+1」~「今天」的範圍查詢，不用額外的sync log(institutional_flows表本身是
symbol+date primary key，INSERT OR REPLACE蓋掉重疊範圍不會出錯)。融資融券/估值/除權息
還是用TPEx官方免費API(只支援「最新一天」，FinMind有沒有等效資料集還沒查證，這次先只換
三大法人這一塊，那是原本卡住的地方)。
"""
import time
from datetime import datetime, timedelta

import requests

from stocks import finmind_client, tpex_client, twse_client
from stocks.config import Config
from stocks.db import (
    add_to_watchlist,
    attach_institutional_flows,
    bars_to_dataframe,
    connect,
    fetch_bars_daily,
    fetch_institutional_flows,
    fetch_synced_market_dates,
    fetch_trading_dates,
    fetch_watchlist,
    init_db,
    insert_bars_daily,
    insert_ex_dividend_schedule,
    insert_institutional_flows,
    insert_margin_balances,
    insert_valuations,
    mark_market_data_synced,
    prune_signal_events,
    set_disabled_strategies,
    upsert_symbol,
)
from stocks.strategy_selection import compute_disabled_strategies
from stocks.yfinance_client import detect_market_and_fetch_bars, fetch_symbol_bars


def _refresh_price_data(config: Config, symbols: list[str]) -> int:
    with connect(config.db_path) as conn:
        dates_before = set(fetch_trading_dates(conn))

    for symbol in symbols:
        bars, market = detect_market_and_fetch_bars(symbol, period="5d")
        if bars:
            with connect(config.db_path) as conn:
                insert_bars_daily(conn, bars)
                upsert_symbol(conn, symbol, market=market, is_watchlist=True)

    with connect(config.db_path) as conn:
        dates_after = set(fetch_trading_dates(conn))
    return len(dates_after - dates_before)


def _refresh_market_data_twse(config: Config, symbols: set[str]) -> int:
    if not symbols:
        return 0

    with connect(config.db_path) as conn:
        all_dates = fetch_trading_dates(conn)
        already_have = fetch_synced_market_dates(conn)
    todo_dates = [d for d in all_dates if d not in already_have]

    # retries=1(不重試)：這條路是dashboard載入時的即時互動路徑，TWSE不穩時應該馬上放棄
    # 改下次(下次dashboard載入或下次跑scripts/fetch_market_data.py)再抓，不該讓使用者
    # 等重試+逾時——那種耐心重試是scripts/fetch_market_data.py長時間背景回補才需要的。
    for date in todo_dates:
        flows = [r for r in twse_client.fetch_institutional_flows_for_date(date, retries=1) if r["symbol"] in symbols]
        time.sleep(config.batch_pacing_seconds)
        margins = [r for r in twse_client.fetch_margin_balances_for_date(date, retries=1) if r["symbol"] in symbols]
        time.sleep(config.batch_pacing_seconds)
        valuations = [r for r in twse_client.fetch_valuations_for_date(date, retries=1) if r["symbol"] in symbols]
        time.sleep(config.batch_pacing_seconds)

        with connect(config.db_path) as conn:
            if flows:
                insert_institutional_flows(conn, flows)
            if margins:
                insert_margin_balances(conn, margins)
            if valuations:
                insert_valuations(conn, valuations)
                for row in valuations:
                    upsert_symbol(conn, row["symbol"], name=row["name"], market="TWSE", is_watchlist=True)
            mark_market_data_synced(conn, [date])

    schedule = [r for r in twse_client.fetch_ex_dividend_schedule(retries=1) if r["symbol"] in symbols]
    with connect(config.db_path) as conn:
        if schedule:
            insert_ex_dividend_schedule(conn, schedule)

    return len(todo_dates)


def _fetched_dates_for_symbols(config: Config, symbols: set[str]) -> set[str]:
    placeholders = ",".join("?" * len(symbols))
    with connect(config.db_path) as conn:
        rows = conn.execute(
            f"SELECT DISTINCT date FROM institutional_flows WHERE symbol IN ({placeholders})",
            tuple(symbols),
        ).fetchall()
    return {r["date"] for r in rows}


def _last_institutional_flow_date(conn, symbol: str) -> str | None:
    row = conn.execute("SELECT MAX(date) AS d FROM institutional_flows WHERE symbol = ?", (symbol,)).fetchone()
    return row["d"] if row and row["d"] else None


def _refresh_market_data_tpex(config: Config, symbols: set[str]) -> bool:
    """三大法人買賣超改用FinMind：每支股票各自查「上次抓到的日期+1」~「今天」，不是
    TPEx官方API那種「只給最新一天」。融資融券/估值/除權息還是走TPEx官方免費API(只能抓
    最新一天)。用前後比對日期集合來判斷是不是真的有新資料(不能只看「有沒有呼叫成功」，
    否則每次開app都會誤報「已更新」)。"""
    if not symbols:
        return False

    dates_before = _fetched_dates_for_symbols(config, symbols)
    today_str = datetime.now().strftime("%Y-%m-%d")

    flows = []
    for symbol in symbols:
        with connect(config.db_path) as conn:
            last_date = _last_institutional_flow_date(conn, symbol)
        start_date = (
            (datetime.strptime(last_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            if last_date
            else today_str
        )
        if start_date <= today_str:
            flows += finmind_client.fetch_institutional_flows_for_range(symbol, start_date, today_str)

    margins = [r for r in tpex_client.fetch_margin_balances_latest() if r["symbol"] in symbols]
    valuations = [r for r in tpex_client.fetch_valuations_latest() if r["symbol"] in symbols]
    schedule = [r for r in tpex_client.fetch_ex_dividend_schedule() if r["symbol"] in symbols]

    with connect(config.db_path) as conn:
        if flows:
            insert_institutional_flows(conn, flows)
        if margins:
            insert_margin_balances(conn, margins)
        if valuations:
            insert_valuations(conn, valuations)
            for row in valuations:
                upsert_symbol(conn, row["symbol"], name=row["name"], market="TPEx", is_watchlist=True)
        if schedule:
            insert_ex_dividend_schedule(conn, schedule)

    dates_after = _fetched_dates_for_symbols(config, symbols)
    return len(dates_after - dates_before) > 0


def _fetch_name_from_recent_valuations(code: str, latest_date: str) -> str:
    """公司名稱不管抓哪一天的估值資料都一樣，不用執著抓latest_date那天——TWSE的每日估值
    報告有公布時間差，股價資料已經有today的K棒時，當天的估值報告可能還沒出來，
    往前找最近幾天(含當天)有資料的那天就好。"""
    date_obj = datetime.strptime(latest_date, "%Y-%m-%d").date()
    for days_back in range(5):
        d = (date_obj - timedelta(days=days_back)).strftime("%Y-%m-%d")
        valuations = [r for r in twse_client.fetch_valuations_for_date(d) if r["symbol"] == code]
        if valuations:
            return valuations[0]["name"]
    return ""


def _contains_chinese(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def _resolve_symbol_input(user_input: str) -> tuple[str | None, str]:
    """使用者在「新增股票代號」欄位除了打代號(例如2330)，也可以直接打中文簡稱(例如
    台積電)——只要有中文字就查TWSE/TPEx官方公司名錄找對應代號，純代號(數字，可能帶
    字母，例如特別股)直接照舊當代號用，不用查名錄。回傳(code, error_message)，code是
    None代表解析失敗，error_message說明原因；解析成功時error_message是空字串。先找
    完全符合"公司簡稱"的，找不到才退而找「名稱裡包含這段文字」的——如果那樣還是找到
    多家(例如打「台」這種太短的字會撞到一大堆)，回傳前5家讓使用者換更精確的名稱重試，
    不要隨便猜一家。

    TWSE/TPEx任一來源連線失敗(TPEx的SSL已知偶爾不穩定，跟run_batch.py同樣的問題)都不該
    讓整個新增流程當掉——只影響那個市場的名稱查得到查不到，另一個市場正常查。"""
    text = user_input.strip()
    if not _contains_chinese(text):
        return text, ""

    directory = []
    sources_ok = 0
    try:
        directory += twse_client.fetch_company_directory()
        sources_ok += 1
    except requests.RequestException:
        pass
    try:
        directory += tpex_client.fetch_company_directory()
        sources_ok += 1
    except requests.RequestException:
        pass
    if sources_ok == 0:
        return None, "查詢公司名錄失敗(TWSE/TPEx連線都逾時或出錯)，請直接輸入股票代號，或稍後再試"

    exact = [d for d in directory if d["name"] == text]
    if exact:
        return exact[0]["symbol"], ""

    partial = [d for d in directory if text in d["name"]]
    if len(partial) == 1:
        return partial[0]["symbol"], ""
    if len(partial) > 1:
        candidates = "、".join(f"{d['name']}({d['symbol']})" for d in partial[:5])
        return None, f"「{text}」對應到多家公司，請輸入更精確的名稱，例如：{candidates}"
    return None, f"找不到「{text}」對應的股票代號，確認名稱是否正確"


def add_symbol_to_watchlist(config: Config, code: str) -> dict:
    """新增一檔股票：自動判斷上市/上櫃，抓近3年股價(跟原本7檔核心觀察清單股票一致)。
    三大法人買賣超：不管上市上櫃都透過FinMind直接補到跟股價一樣的3年歷史(FinMind的
    TaiwanStockInstitutionalInvestorsBuySell資料集兩個市場都涵蓋，一次API呼叫涵蓋整段
    範圍，不用像TWSE官方API那樣逐日查詢)——原本上市只抓最新一天，是因為sync log是用
    「日期」而非「日期+股票」為單位在追蹤，其他股票已經讓那些日期標記成「抓過了」，
    新股票不會自動觸發回頭補值，2026-08-08改成兩個市場都直接用FinMind繞開這個限制，不用
    再手動跑`fetch_market_data.py --full`才能讓新股票的籌碼類策略(chip_momentum等)有
    完整樣本可以判斷。FinMind失敗的話(額度/連線問題)退回只抓最新一天，不讓新增股票整個
    失敗。融資融券/估值兩個市場一樣只先抓最新一天不變——這兩個沒有被任何策略用到(只有
    dashboard「籌碼/基本面」頁籤顯示用)，之後每天累積即可，不值得為此犧牲新增股票的
    等待時間。

    code參數也接受中文簡稱(例如「台積電」)，會先透過_resolve_symbol_input()解析成代號
    再往下走，跟純打代號是同一條路徑、同一個結果。"""
    code, resolve_error = _resolve_symbol_input(code)
    if code is None:
        return {"ok": False, "message": resolve_error}
    init_db(config.db_path)

    with connect(config.db_path) as conn:
        already_in = conn.execute(
            "SELECT 1 FROM symbols WHERE code = ? AND is_watchlist = 1", (code,)
        ).fetchone()
    if already_in:
        return {"ok": False, "message": f"{code} 已經在觀察清單裡了"}

    bars, market = detect_market_and_fetch_bars(code, period="3y")
    if not bars:
        return {"ok": False, "message": f"抓不到 {code} 的股價資料，確認代號是否正確"}

    with connect(config.db_path) as conn:
        insert_bars_daily(conn, bars)

    earliest_date = min(b.ts for b in bars).strftime("%Y-%m-%d")
    latest_date = max(b.ts for b in bars).strftime("%Y-%m-%d")

    try:
        flows = finmind_client.fetch_institutional_flows_for_range(code, earliest_date, latest_date)
        chips_note = "三大法人已透過FinMind補到近3年歷史；融資融券/估值只先抓最新一天，之後每天累積"
    except requests.RequestException:
        flows = []
        chips_note = "三大法人歷史回補失敗(FinMind連線問題)，先只有之後每天累積的資料；融資融券/估值只先抓最新一天"

    if market == "TPEx":
        margins = [r for r in tpex_client.fetch_margin_balances_latest() if r["symbol"] == code]
        valuations = [r for r in tpex_client.fetch_valuations_latest() if r["symbol"] == code]
    else:
        margins = [r for r in twse_client.fetch_margin_balances_for_date(latest_date) if r["symbol"] == code]
        valuations = [r for r in twse_client.fetch_valuations_for_date(latest_date) if r["symbol"] == code]

    if valuations:
        name = valuations[0]["name"]
    elif market == "TWSE":
        # 當天股價K棒已經有了，但TWSE的每日估值報告可能還沒公布，往前找最近幾天的估值資料要名字
        name = _fetch_name_from_recent_valuations(code, latest_date)
    else:
        name = ""

    with connect(config.db_path) as conn:
        add_to_watchlist(conn, code, name=name, market=market)
        if flows:
            insert_institutional_flows(conn, flows)
        if margins:
            insert_margin_balances(conn, margins)
        if valuations:
            insert_valuations(conn, valuations)

    # 新增當下立刻跑一次排除評估，不用等到下個月的排程——兩個市場這時三大法人都已經有
    # 3年歷史(FinMind)，chip_momentum/golden_cross_scaleout這種靠籌碼判斷的策略可以馬上
    # 判斷；如果FinMind剛好失敗(見上面的try/except)，flows還是只有最新一天，樣本不足就
    # 先排除(見strategy_selection.should_disable)，等之後每月排程重跑、資料累積夠了才會
    # 真正開始判斷，不是預設先開著。
    with connect(config.db_path) as conn:
        symbol_bars = bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date")
        symbol_bars = attach_institutional_flows(symbol_bars, fetch_institutional_flows(conn, code))
        disabled = compute_disabled_strategies(code, symbol_bars, config.strategy_params)
        set_disabled_strategies(conn, code, disabled)

    label = f"{code}（{name}）" if name else code
    market_label = "上市" if market == "TWSE" else "上櫃"
    return {
        "ok": True,
        "message": f"已新增 {label}（{market_label}），近3年股價已抓好。{chips_note}",
    }


def should_check_for_updates(last_check: datetime | None, now: datetime, cutoff_hour: int = 19) -> bool:
    """dashboard開啟時要不要跑check_and_update()——盤後資料通常要到晚上cutoff_hour點
    (預設19點)才會齊，一天只需要真的檢查一次；但如果上次檢查是cutoff_hour點以前、現在
    已經過了cutoff_hour點，就該再檢查一次抓當天的新資料。呼叫端(dashboard/app.py)要把
    「上次檢查時間」存進DB(不能用st.session_state，那個瀏覽器重新整理就會重置，等於
    每次刷新都會重新檢查一次，完全沒有省到)。"""
    if last_check is None:
        return True
    if last_check.date() < now.date():
        return True
    today_cutoff = now.replace(hour=cutoff_hour, minute=0, second=0, microsecond=0)
    return now >= today_cutoff and last_check < today_cutoff


def check_and_update(config: Config) -> dict:
    """比對DB裡已有的交易日跟資料，缺什麼就抓什麼，沒有新資料就什麼都不做。"""
    init_db(config.db_path)

    with connect(config.db_path) as conn:
        rows = fetch_watchlist(conn)
    if not rows:
        return {"watchlist_empty": True, "new_price_days": 0, "new_market_days": 0, "otc_synced": False, "errors": []}

    watchlist = {r["code"] for r in rows}
    twse_symbols = {r["code"] for r in rows if r["market"] != "TPEx"}
    tpex_symbols = {r["code"] for r in rows if r["market"] == "TPEx"}

    errors = []
    new_price_days = 0
    new_market_days = 0
    otc_synced = False

    # 每個資料來源各自獨立try/except：任何一個外部API連線失敗(逾時/SSL/伺服器錯誤)都不該讓
    # 整個dashboard打不開，只影響那一塊資料，其他照樣正常更新。
    try:
        new_price_days = _refresh_price_data(config, sorted(watchlist))
    except requests.RequestException as exc:
        errors.append(f"股價更新失敗：{exc}")

    try:
        new_market_days = _refresh_market_data_twse(config, twse_symbols)
    except requests.RequestException as exc:
        errors.append(f"上市籌碼更新失敗：{exc}")

    try:
        otc_synced = _refresh_market_data_tpex(config, tpex_symbols)
    except requests.RequestException as exc:
        errors.append(f"上櫃籌碼更新失敗：{exc}")

    with connect(config.db_path) as conn:
        prune_signal_events(conn)

    return {
        "watchlist_empty": False,
        "new_price_days": new_price_days,
        "new_market_days": new_market_days,
        "otc_synced": otc_synced,
        "errors": errors,
    }
