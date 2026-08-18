"""供dashboard在開啟時做一次「有新資料就抓，沒有就跳過」的檢查，
重用跟scripts/fetch_historical.py、scripts/fetch_market_data.py一樣的底層client/db函式，
只是包成一個安靜、不印進度的版本，適合在網頁載入時跑。

上市(TWSE)跟上櫃(TPEx)的籌碼資料走不同邏輯：三大法人/估值都改用FinMind(可以指定日期
範圍查詢)，TWSE用sync log追蹤還缺哪些日期backfill，TPEx用「上次抓到的日期+1」~「今天」
的範圍查詢，不用額外的sync log(institutional_flows/valuations兩張表都是symbol+date
primary key，INSERT OR REPLACE蓋掉重疊範圍不會出錯)。2026-08-13把TPEx的估值也從官方
免費API換成FinMind——官方API(www.tpex.org.tw)常有SSL憑證問題(對方伺服器憑證本身的
問題)，FinMind穩定得多。融資融券資料2026-08-16拿掉(dashboard圖表用不到，改成成交量
子圖)，不再抓取。

月營收(monthly_revenue)：2026-08-16新增，基本面濾網研究用。跟三大法人/估值不同的是
不分上市/上櫃，兩個市場共用同一個FinMind dataset(`TaiwanStockMonthRevenue`)，`_refresh_
monthly_revenue`對整個觀察清單(不分市場)用同一套`_fetch_range_per_symbol`。目前還沒有
任何策略讀取這份資料。

除權息預告表(ex_dividend_schedule)：2026-08-18改用FinMind的`TaiwanStockDividend`(完整
歷史)取代原本的TWSE/TPEx官方「預告表」API——後者只列出「已公告但還沒發生」的除權息，
事件一過期就從清單上消失、不是歷史紀錄，也一樣會偶爾撞到TPEx官方API那個已知的SSL憑證
問題(原本以為這個endpoint沒有這個問題，後來查證發現一樣會發生)。跟月營收一樣不分上市/
上櫃，但`_refresh_ex_dividend_schedule`**不用**`_fetch_range_per_symbol`的日期書籤機制
(見該函式docstring說明原因)，每次都重查整段股價歷史涵蓋的範圍。
"""
import time
from datetime import datetime, timedelta

import requests

from stocks import finmind_client, tpex_client, twse_client
from stocks.config import Config
from stocks.db import (
    add_to_watchlist,
    attach_institutional_flows,
    attach_monthly_revenue_growth,
    bars_to_dataframe,
    connect,
    export_watchlist_snapshot,
    fetch_bars_daily,
    fetch_institutional_flows,
    fetch_monthly_revenue,
    fetch_synced_market_dates,
    fetch_trading_dates,
    fetch_watchlist,
    init_db,
    insert_bars_daily,
    insert_ex_dividend_schedule,
    insert_institutional_flows,
    insert_monthly_revenue,
    insert_valuations,
    mark_market_data_synced,
    prune_signal_events,
    set_disabled_strategies,
    set_industry_code,
    upsert_industry_universe,
    upsert_symbol,
    watchlist_sync_path,
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
        valuations = [r for r in twse_client.fetch_valuations_for_date(date, retries=1) if r["symbol"] in symbols]
        time.sleep(config.batch_pacing_seconds)

        with connect(config.db_path) as conn:
            if flows:
                insert_institutional_flows(conn, flows)
            if valuations:
                insert_valuations(conn, valuations)
                for row in valuations:
                    upsert_symbol(conn, row["symbol"], name=row["name"], market="TWSE", is_watchlist=True)
            mark_market_data_synced(conn, [date])

    return len(todo_dates)


def _fetched_dates_for_symbols(config: Config, symbols: set[str], table: str = "institutional_flows") -> set[str]:
    placeholders = ",".join("?" * len(symbols))
    with connect(config.db_path) as conn:
        rows = conn.execute(
            f"SELECT DISTINCT date FROM {table} WHERE symbol IN ({placeholders})",
            tuple(symbols),
        ).fetchall()
    return {r["date"] for r in rows}


def _last_date(conn, table: str, symbol: str) -> str | None:
    row = conn.execute(f"SELECT MAX(date) AS d FROM {table} WHERE symbol = ?", (symbol,)).fetchone()
    return row["d"] if row and row["d"] else None


def _last_institutional_flow_date(conn, symbol: str) -> str | None:
    return _last_date(conn, "institutional_flows", symbol)


def _fetch_range_per_symbol(config: Config, symbols: set[str], table: str, fetch_fn, today_str: str) -> list[dict]:
    """幫每支股票各自查「上次抓到的日期(該table)+1」~「今天」，不是TPEx官方API那種
    「只給最新一天」——三大法人/估值都是同一套邏輯，只是查的table/FinMind
    dataset不同，抽成共用函式避免兩份幾乎一樣的迴圈。"""
    rows = []
    for symbol in symbols:
        with connect(config.db_path) as conn:
            last_date = _last_date(conn, table, symbol)
        start_date = (
            (datetime.strptime(last_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            if last_date
            else today_str
        )
        if start_date <= today_str:
            rows += fetch_fn(symbol, start_date, today_str)
    return rows


def _refresh_monthly_revenue(config: Config, symbols: set[str]) -> bool:
    """月營收(monthly_revenue)跟三大法人/估值不一樣：TaiwanStockMonthRevenue不分上市/
    上櫃，兩個市場共用同一個FinMind dataset，不需要像institutional_flows/valuations
    那樣依市場分成TWSE官方API/TPEx FinMind兩條路——這裡對整個觀察清單(不分市場)用同一套
    `_fetch_range_per_symbol`(跟TPEx那條路共用同一個helper，只是table/fetch_fn換成
    monthly_revenue/fetch_monthly_revenue_for_range)。月營收本身一個月才更新一次，
    這裡沒有額外做「本月是否已經抓過」的節流判斷——`_fetch_range_per_symbol`本來就是
    查「上次抓到的日期+1」~「今天」，同一個月內重複執行時FinMind會回傳空結果(還沒有
    下個月的資料)，INSERT OR REPLACE也不會製造重複，多跑幾次沒有副作用，不需要額外
    的節流機制。"""
    if not symbols:
        return False

    dates_before = _fetched_dates_for_symbols(config, symbols, table="monthly_revenue")
    today_str = datetime.now().strftime("%Y-%m-%d")
    try:
        revenue_rows = _fetch_range_per_symbol(
            config, symbols, "monthly_revenue", finmind_client.fetch_monthly_revenue_for_range, today_str
        )
    except requests.RequestException:
        revenue_rows = []

    with connect(config.db_path) as conn:
        if revenue_rows:
            insert_monthly_revenue(conn, revenue_rows)

    dates_after = _fetched_dates_for_symbols(config, symbols, table="monthly_revenue")
    return len(dates_after - dates_before) > 0


def _refresh_ex_dividend_schedule(config: Config, symbols: set[str]) -> None:
    """除權息預告表2026-08-18起改用FinMind的TaiwanStockDividend(完整歷史)取代TWSE/TPEx
    官方「預告表」API——後者只列出「已公告但還沒發生」的除權息，事件一過期就從清單上消失，
    不是歷史紀錄。這個除息假停損修正功能2026-08-15才新增，導致今年6~8月已經發生的除權息
    事件從一開始就沒機會被預告表捕捉到(使用者查證發現落差達24筆)；改用FinMind可以直接
    回補這個落差，之後也不用擔心預告表把過去的資料丟掉。

    跟三大法人/估值/月營收不同的是**不用**`_fetch_range_per_symbol`的「上次抓到的日期+1」
    書籤機制——那個機制假設書籤欄位(date)只會往過去累積，但這裡的書籤候選欄位是ex_date，
    常常是還沒發生的未來日期(例如某支股票已知下一次除息在兩週後)，如果拿它當下次查詢的
    起點，會跳過決議公告時間更早、但除權息日期還沒到的下一輪公告(FinMind的start_date/
    end_date過濾的是決議/公告日期，不是ex_date本身)。改成每次都重查整段股價歷史涵蓋的
    範圍——check_and_update()一天最多真的觸發兩次，全觀察清單成本很低，INSERT OR REPLACE
    本身就是idempotent，不會因為重複查詢而製造重複資料。跟月營收一樣不分上市/上櫃，兩個
    市場共用同一個dataset。"""
    if not symbols:
        return

    with connect(config.db_path) as conn:
        all_dates = fetch_trading_dates(conn)
    if not all_dates:
        return
    start_date, end_date = all_dates[0], all_dates[-1]

    schedule = []
    for symbol in symbols:
        schedule += finmind_client.fetch_ex_dividend_schedule_for_range(symbol, start_date, end_date)

    with connect(config.db_path) as conn:
        if schedule:
            insert_ex_dividend_schedule(conn, schedule)


def _refresh_market_data_tpex(config: Config, symbols: set[str]) -> bool:
    """三大法人/估值都改用FinMind：每支股票各自查「上次抓到的日期+1」~「今天」，
    不是TPEx官方API那種「只給最新一天」——2026-08-13把估值也換掉，原因是
    TPEx官方免費API(www.tpex.org.tw)常有SSL憑證問題(Missing Subject Key Identifier，
    對方伺服器憑證本身的問題，不是我們這邊能修的)，兩個資料源各自獨立try/except，任一個
    失敗不影響另一個。除權息預告表2026-08-18也改用FinMind(見_refresh_ex_dividend_schedule)，
    原本以為「這個endpoint沒觀察到SSL問題」，後來實際查證發現一樣會偶爾撞到同一個SSL
    問題，跟三大法人/估值同一個理由換掉。FinMind沒有公司名稱欄位，不影響——名稱在新增
    股票當下就抓過了，之後不會變，不需要每天重新確認。用前後比對日期集合來判斷是不是
    真的有新資料(不能只看「有沒有呼叫成功」，否則每次開app都會誤報「已更新」)。"""
    if not symbols:
        return False

    dates_before = _fetched_dates_for_symbols(config, symbols)
    today_str = datetime.now().strftime("%Y-%m-%d")

    try:
        flows = _fetch_range_per_symbol(
            config, symbols, "institutional_flows", finmind_client.fetch_institutional_flows_for_range, today_str
        )
    except requests.RequestException:
        flows = []

    try:
        valuations = _fetch_range_per_symbol(
            config, symbols, "valuations", finmind_client.fetch_valuations_for_range, today_str
        )
    except requests.RequestException:
        valuations = []

    with connect(config.db_path) as conn:
        if flows:
            insert_institutional_flows(conn, flows)
        if valuations:
            insert_valuations(conn, valuations)

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
    """新增一檔股票：自動判斷上市/上櫃，抓近10年股價(跟觀察清單其他股票一致——2026-08-17
    使用者把回測期間從3年拉長到10年後，新股票也該用同樣長度，不然樣本會比其他股票明顯少)。
    三大法人買賣超：不管上市上櫃都透過FinMind直接補到跟股價一樣的10年歷史
    (TaiwanStockInstitutionalInvestorsBuySell這個dataset涵蓋兩個市場，一次API呼叫涵蓋
    整段範圍，不用像TWSE官方API那樣逐日查詢)——原本上市只抓最新一天，是因為sync log是用
    「日期」而非「日期+股票」為單位在追蹤，其他股票已經讓那些日期標記成「抓過了」，新股票
    不會自動觸發回頭補值，2026-08-08改成兩個市場都直接用FinMind繞開這個限制，不用再手動跑
    `fetch_market_data.py --full`才能讓新股票的籌碼類策略(chip_momentum等)有完整樣本可以
    判斷。FinMind失敗的話(額度/連線問題)退回只抓最新一天，不讓新增股票整個失敗。

    估值(PE/殖利率/PB)：2026-08-16之前這裡只抓最新一天，docstring寫著「FinMind有沒有
    等效資料集還沒查證」——後來發現`finmind_client.fetch_valuations_for_range()`
    (TaiwanStockPER)其實早就存在、而且`_fetch_range_per_symbol`那條既有股票每日增量
    回補的路徑本來就在用它，只是新增股票這裡沒接上，純粹是漏掉，不是真的沒有對應
    API。跟三大法人同一套try/except容錯(FinMind失敗就退回只有當天快照)，兩個
    FinMind呼叫各自獨立try/except，其中一個失敗不影響另一個。當天的估值快照(有
    公司名稱可以用，FinMind的PER dataset沒有名稱欄位)還是照舊從TWSE/TPEx官方API抓，
    只是現在會在FinMind回補的10年歷史"之後"再寫入一次，確保當天這筆用的是官方來源、
    不會被FinMind同一天的資料蓋掉。

    月營收(monthly_revenue)：2026-08-16新增，基本面濾網研究用——`TaiwanStockMonthRevenue`
    不分上市/上櫃，兩個市場都適用同一個dataset，不用像三大法人/估值那樣分開處理。跟前兩者
    同一套try/except容錯，三個FinMind呼叫各自獨立，任一個失敗不影響其他兩個。目前還沒有
    任何策略讀取這份資料，純粹先把歷史補齊供之後設計濾網/回測用。

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

    bars, market = detect_market_and_fetch_bars(code, period="10y")
    if not bars:
        return {"ok": False, "message": f"抓不到 {code} 的股價資料，確認代號是否正確"}

    with connect(config.db_path) as conn:
        insert_bars_daily(conn, bars)

    earliest_date = min(b.ts for b in bars).strftime("%Y-%m-%d")
    latest_date = max(b.ts for b in bars).strftime("%Y-%m-%d")

    try:
        flows = finmind_client.fetch_institutional_flows_for_range(code, earliest_date, latest_date)
        flows_note = "三大法人已透過FinMind補到近10年歷史"
    except requests.RequestException:
        flows = []
        flows_note = "三大法人歷史回補失敗(FinMind連線問題)，先只有之後每天累積的資料"

    try:
        valuation_history = finmind_client.fetch_valuations_for_range(code, earliest_date, latest_date)
        valuation_note = "估值(PE/殖利率/PB)已透過FinMind補到近10年歷史"
    except requests.RequestException:
        valuation_history = []
        valuation_note = "估值歷史回補失敗(FinMind連線問題)，先只有之後每天累積的資料"

    try:
        revenue_history = finmind_client.fetch_monthly_revenue_for_range(code, earliest_date, latest_date)
        revenue_note = "月營收已透過FinMind補到近10年歷史"
    except requests.RequestException:
        revenue_history = []
        revenue_note = "月營收歷史回補失敗(FinMind連線問題)，先只有之後每月累積的資料"

    try:
        ex_dividend_history = finmind_client.fetch_ex_dividend_schedule_for_range(code, earliest_date, latest_date)
        ex_dividend_note = "除權息已透過FinMind補到近10年歷史"
    except requests.RequestException:
        ex_dividend_history = []
        ex_dividend_note = "除權息歷史回補失敗(FinMind連線問題)，先只有之後累積的資料"

    chips_note = f"{flows_note}；{valuation_note}；{revenue_note}；{ex_dividend_note}"

    if market == "TPEx":
        # 這裡故意還是打tpex_client(不是finmind_client)，因為新股票的名稱要從這個回應的
        # "name"欄位取得(FinMind的估值dataset沒有公司名稱)；用try/except包起來是因為
        # www.tpex.org.tw常有SSL憑證問題(見上面三大法人的說明)，失敗時退回
        # name=""(不是讓新增股票整個crash)——2026-08-13修正，之前這裡沒有防護。
        try:
            valuations = [r for r in tpex_client.fetch_valuations_latest() if r["symbol"] == code]
        except requests.RequestException:
            valuations = []
    else:
        valuations = [r for r in twse_client.fetch_valuations_for_date(latest_date) if r["symbol"] == code]

    if valuations:
        name = valuations[0]["name"]
    elif market == "TWSE":
        # 當天股價K棒已經有了，但TWSE的每日估值報告可能還沒公布，往前找最近幾天的估值資料要名字
        name = _fetch_name_from_recent_valuations(code, latest_date)
    else:
        name = ""

    if not name:
        # 2026-08-16發現：上面兩個官方名稱來源都失敗時(尤其TPEx的SSL問題)，還有FinMind
        # 的TaiwanStockInfo這個dataset可以當最後備援(不是fetch_valuations_for_range用的
        # TaiwanStockPER，那個真的沒有名稱欄位)——同一個try/except容錯慣例，失敗一樣
        # 退回name=""，不讓新增股票整個crash。
        try:
            name = finmind_client.fetch_stock_name(code)
        except requests.RequestException:
            name = ""

    # 順便標記官方產業分類代碼給circuit_breaker.py用，並把「全市場同產業」的股票也一起
    # 拉進來(is_watchlist=0，不會出現在觀察清單畫面)——不能只查這支股票自己的分類就好，
    # 不然如果這支股票剛好是觀察清單目前還沒有的產業，斷路器會因為完全沒有同業資料可以
    # 算寬度，等於形同沒開。兩個市場都抓(不是只抓這支股票自己上市/上櫃的那邊)是因為同一個
    # 產業代碼底下上市上櫃都有成員，跟scripts/populate_industry_codes.py同一套邏輯，
    # 這裡是「新增單一股票時自動觸發同一件事」，不用再手動重跑那支腳本。兩個來源各自獨立
    # try/except(TPEx的SSL已知偶爾不穩定，跟上面估值查詢同一套容錯慣例)，任一個失敗不影響
    # 新增流程本身，只是那個市場的同業清單這次沒補到，之後新增股票或手動重跑populate_
    # industry_codes.py時還會再試。
    directory = []
    try:
        directory += [{**d, "market": "TWSE"} for d in twse_client.fetch_company_directory()]
    except requests.RequestException:
        pass
    try:
        directory += [{**d, "market": "TPEx"} for d in tpex_client.fetch_company_directory()]
    except requests.RequestException:
        pass
    industry_code = next((d.get("industry_code") for d in directory if d["symbol"] == code), None)

    with connect(config.db_path) as conn:
        add_to_watchlist(conn, code, name=name, market=market)
        if industry_code:
            set_industry_code(conn, code, industry_code)
            # 排除這支股票自己(symbol != code)：它自己的name已經由上面的valuations流程
            # 決定好了(可能刻意是""，等下次每日更新才補上)，這裡只負責補其他同業peer的
            # 資料，不能讓peer upsert順便把這支股票自己的name蓋掉。
            peers = [
                {"code": d["symbol"], "name": d["name"], "market": d["market"], "industry_code": d["industry_code"]}
                for d in directory
                if d.get("industry_code") == industry_code and d["symbol"] != code
            ]
            upsert_industry_universe(conn, peers)
        export_watchlist_snapshot(conn, watchlist_sync_path(config.db_path))
        if flows:
            insert_institutional_flows(conn, flows)
        if valuation_history:
            insert_valuations(conn, valuation_history)
        if valuations:
            # 官方來源(有名稱)的當天快照最後寫入，確保今天這筆不會被上面FinMind範圍
            # 回補的同一天資料覆蓋掉(INSERT OR REPLACE以後寫入的為準)。
            insert_valuations(conn, valuations)
        if revenue_history:
            insert_monthly_revenue(conn, revenue_history)
        if ex_dividend_history:
            insert_ex_dividend_schedule(conn, ex_dividend_history)

    # 新增當下立刻跑一次排除評估，不用等到下個月的排程——兩個市場這時三大法人都已經有
    # 10年歷史(FinMind)，chip_momentum/golden_cross這種靠籌碼判斷的策略可以馬上
    # 判斷；如果FinMind剛好失敗(見上面的try/except)，flows還是只有最新一天，樣本不足就
    # 先排除(見strategy_selection.should_disable)，等之後每月排程重跑、資料累積夠了才會
    # 真正開始判斷，不是預設先開著。
    with connect(config.db_path) as conn:
        symbol_bars = bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date")
        symbol_bars = attach_institutional_flows(symbol_bars, fetch_institutional_flows(conn, code))
        symbol_bars = attach_monthly_revenue_growth(symbol_bars, [dict(r) for r in fetch_monthly_revenue(conn, code)])
        disabled = compute_disabled_strategies(code, symbol_bars, config.strategy_params)
        set_disabled_strategies(conn, code, disabled)

    label = f"{code}（{name}）" if name else code
    market_label = "上市" if market == "TWSE" else "上櫃"
    return {
        "ok": True,
        "message": f"已新增 {label}（{market_label}），近10年股價已抓好。{chips_note}",
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


def is_market_open_now(config: Config, now: datetime) -> bool:
    """判斷now是不是台股交易時段(週一到週五、config.market_open~market_close之間)——
    2026-08-17使用者要求：dashboard的30秒自動更新只在開盤時段才有意義，收盤後股價/籌碼
    資料不會變，重複輪詢(尤其是Shioaji現場連線)是白工，該關掉。不含國定假日行事曆(這個
    專案目前沒有假日資料來源)，遇到國定假日還是會誤判成「開盤」，代價只是那幾天白跑
    幾次30秒輪詢，不影響任何資料正確性(輪詢本來就是唯讀，重跑只是浪費，不會算錯)。"""
    if now.weekday() >= 5:  # 5=Saturday, 6=Sunday
        return False
    open_time = datetime.strptime(config.market_open, "%H:%M").time()
    close_time = datetime.strptime(config.market_close, "%H:%M").time()
    return open_time <= now.time() <= close_time


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

    revenue_synced = False
    try:
        revenue_synced = _refresh_monthly_revenue(config, watchlist)
    except requests.RequestException as exc:
        errors.append(f"月營收更新失敗：{exc}")

    try:
        _refresh_ex_dividend_schedule(config, watchlist)
    except requests.RequestException as exc:
        errors.append(f"除權息預告表更新失敗：{exc}")

    with connect(config.db_path) as conn:
        prune_signal_events(conn)

    return {
        "watchlist_empty": False,
        "new_price_days": new_price_days,
        "new_market_days": new_market_days,
        "otc_synced": otc_synced,
        "revenue_synced": revenue_synced,
        "errors": errors,
    }
