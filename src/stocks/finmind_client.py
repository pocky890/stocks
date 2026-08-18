"""FinMind開源API(https://finmindtrade.com)：三大法人買賣超歷史資料。主要用來補上櫃
(TPEx)的歷史——TPEx自己的免費API只給「最新一天」，沒辦法像twse_client.py那樣逐日回補
(查證過，官方OpenAPI 225個endpoint都不接受任何查詢參數)。FinMind這個dataset本身就是
start_date~end_date範圍查詢，一次呼叫就能拿到整段歷史，不用像TWSE那樣逐日呼叫。免登入
限制每小時300次，一次呼叫就能拿完一支股票全部歷史，個人用量遠遠用不到這個上限。

foreign_net/trust_net/dealer_net的分類跟twse_client.py一致(方便同一張institutional_flows
表混用兩邊來源不會產生語意衝突)：外資(含外資自營商)歸foreign_net、投信歸trust_net、
自營商(自行+避險)歸dealer_net、total_net是三者合計(FinMind沒有像TWSE T86report那樣現成的
合計欄位，這裡自己加總)。
"""
import requests

TIMEOUT = 15
BASE_URL = "https://api.finmindtrade.com/api/v4/data"
DATASET = "TaiwanStockInstitutionalInvestorsBuySell"
VALUATION_DATASET = "TaiwanStockPER"
STOCK_INFO_DATASET = "TaiwanStockInfo"
MONTHLY_REVENUE_DATASET = "TaiwanStockMonthRevenue"
DIVIDEND_DATASET = "TaiwanStockDividend"

FOREIGN_NAMES = {"Foreign_Investor", "Foreign_Dealer_Self"}
TRUST_NAMES = {"Investment_Trust"}
DEALER_NAMES = {"Dealer_self", "Dealer_Hedging"}


def _fetch_range(dataset: str, symbol: str, start_date: str, end_date: str) -> list[dict]:
    resp = requests.get(
        BASE_URL,
        params={"dataset": dataset, "data_id": symbol, "start_date": start_date, "end_date": end_date},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("msg") != "success":
        return []
    return payload.get("data", [])


def fetch_institutional_flows_for_range(symbol: str, start_date: str, end_date: str) -> list[dict]:
    """回傳[{symbol, date, foreign_net, trust_net, dealer_net, total_net}, ...]，
    日期照升序排列，一次涵蓋start_date~end_date整段範圍。"""
    by_date: dict[str, dict[str, int]] = {}
    for row in _fetch_range(DATASET, symbol, start_date, end_date):
        bucket = by_date.setdefault(row["date"], {"foreign": 0, "trust": 0, "dealer": 0})
        net = row["buy"] - row["sell"]
        name = row["name"]
        if name in FOREIGN_NAMES:
            bucket["foreign"] += net
        elif name in TRUST_NAMES:
            bucket["trust"] += net
        elif name in DEALER_NAMES:
            bucket["dealer"] += net

    return [
        {
            "symbol": symbol,
            "date": date,
            "foreign_net": b["foreign"],
            "trust_net": b["trust"],
            "dealer_net": b["dealer"],
            "total_net": b["foreign"] + b["trust"] + b["dealer"],
        }
        for date, b in sorted(by_date.items())
    ]


def fetch_valuations_for_range(symbol: str, start_date: str, end_date: str) -> list[dict]:
    """回傳[{symbol, date, pe_ratio, dividend_yield, pb_ratio}, ...]，欄位跟
    twse_client.fetch_valuations_for_date對齊——但FinMind的TaiwanStockPER沒有股票名稱欄位，
    這裡不像twse那邊順便回傳name，backfill時symbols表的name已經在watchlist加入時寫過了，
    不需要靠這個dataset補。"""
    return [
        {
            "symbol": symbol,
            "date": row["date"],
            "pe_ratio": row["PER"],
            "dividend_yield": row["dividend_yield"],
            "pb_ratio": row["PBR"],
        }
        for row in _fetch_range(VALUATION_DATASET, symbol, start_date, end_date)
    ]


def fetch_monthly_revenue_for_range(symbol: str, start_date: str, end_date: str) -> list[dict]:
    """回傳[{symbol, date, revenue_year, revenue_month, revenue}, ...]，日期升序排列。
    TaiwanStockMonthRevenue不分上市/上櫃，兩個市場都適用同一個dataset，不用像
    institutional_flows/valuations那樣分開處理。

    2026-08-16基本面濾網研究：這個dataset的"date"欄位是「營收所屬月份的下個月1號」
    (例如2025年1月營收的date是2025-02-01)，不是公司實際公告日期——公司法規公告
    期限是次月10日前，所以用這個date當作「這筆資料何時可以取得」的近似值，會提早
    最多約9天知道這筆營收，用於策略回測時要注意這個微小的look-ahead——目前這份資料
    還沒有任何策略讀取，之後設計進場濾網時要處理這個時間差(用date往後加幾天當作真正
    可用日期，或直接用月底當保守估計)，不能假設date當天就查得到。"""
    return [
        {
            "symbol": symbol,
            "date": row["date"],
            "revenue_year": row["revenue_year"],
            "revenue_month": row["revenue_month"],
            "revenue": row["revenue"],
        }
        for row in _fetch_range(MONTHLY_REVENUE_DATASET, symbol, start_date, end_date)
    ]


def fetch_ex_dividend_schedule_for_range(symbol: str, start_date: str, end_date: str) -> list[dict]:
    """回傳[{symbol, ex_date, cash_dividend, stock_dividend_ratio, detail}, ...]，日期升序
    排列。2026-08-18取代原本的twse_client/tpex_client.fetch_ex_dividend_schedule()——那兩個
    是TWSE/TPEx官方「預告表」API，只列出「已公告但還沒發生」的除權息，事件一過期就從清單上
    消失，不是歷史資料。這個除息修正功能2026-08-15才新增，導致當時已經過期的除權息事件
    (即使今年稍早才發生)從一開始就沒機會被預告表捕捉到。TaiwanStockDividend是完整歷史
    dataset，不會有這個問題，可以一次回補過去的落差，之後也不用擔心預告表把過去資料丟掉。

    這個dataset一筆記錄對應一次股利分派決議，同時可能有現金股利跟股票股利各自的除權/
    除息日(CashExDividendTradingDate/StockExDividendTradingDate，日期通常不同，也可能
    其中一個是空字串代表這次沒有這項)——用這裡的date參數(start_date/end_date)過濾的
    是決議/公告日期，不是除權息日期本身，所以不能把某支股票「目前已知最後一個除權息日」
    當作下次查詢的起點(那個日期常常在未來，用它當書籤會跳過決議日期更早、但除權息日期
    還沒到的下一輪公告)——因此呼叫端每次都查整段涵蓋範圍，不做增量書籤。"""
    by_date: dict[str, dict] = {}
    for row in _fetch_range(DIVIDEND_DATASET, symbol, start_date, end_date):
        cash_date = row.get("CashExDividendTradingDate") or None
        if cash_date:
            by_date.setdefault(cash_date, {})["cash_dividend"] = row.get("CashEarningsDistribution")
        stock_date = row.get("StockExDividendTradingDate") or None
        if stock_date:
            by_date.setdefault(stock_date, {})["stock_dividend_ratio"] = row.get("StockEarningsDistribution")

    rows = []
    for ex_date, fields in sorted(by_date.items()):
        has_cash = "cash_dividend" in fields
        has_stock = "stock_dividend_ratio" in fields
        detail = "除權息" if has_cash and has_stock else "除權" if has_stock else "除息"
        rows.append(
            {
                "symbol": symbol,
                "ex_date": ex_date,
                "cash_dividend": fields.get("cash_dividend"),
                "stock_dividend_ratio": f"{fields['stock_dividend_ratio']:.8f}" if has_stock else None,
                "detail": detail,
            }
        )
    return rows


def fetch_stock_name(symbol: str) -> str:
    """回傳這支股票的中文簡稱，查不到就回傳空字串。2026-08-16發現：TaiwanStockPER(見上面
    fetch_valuations_for_range的說明)確實沒有名稱欄位，但TaiwanStockInfo這個不同的
    dataset有——可以在daily_update.add_symbol_to_watchlist查完官方名稱API(TWSE/TPEx)
    後當最後備援，尤其TPEx官方API(www.tpex.org.tw)常因為對方SSL憑證問題整組查不到
    (同一個問題已經讓institutional/valuation兩個TPEx資料源都改用FinMind，見
    daily_update.py開頭2026-08-13那段說明，名稱查詢當時漏了改)。這個dataset不接受
    日期範圍查詢，只回傳「目前」的名稱，但公司名稱本來就幾乎不會變，不需要指定日期。"""
    resp = requests.get(BASE_URL, params={"dataset": STOCK_INFO_DATASET, "data_id": symbol}, timeout=TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("msg") != "success":
        return ""
    rows = payload.get("data", [])
    return rows[0]["stock_name"] if rows else ""
