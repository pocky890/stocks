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
MARGIN_DATASET = "TaiwanStockMarginPurchaseShortSale"
VALUATION_DATASET = "TaiwanStockPER"
STOCK_INFO_DATASET = "TaiwanStockInfo"

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


def fetch_margin_balances_for_range(symbol: str, start_date: str, end_date: str) -> list[dict]:
    """回傳[{symbol, date, margin_buy, margin_sell, margin_balance, short_buy, short_sell,
    short_balance}, ...]，欄位跟twse_client.fetch_margin_balances_for_date對齊，方便
    insert_margin_balances不用管資料來源是哪一邊。"""
    return [
        {
            "symbol": symbol,
            "date": row["date"],
            "margin_buy": row["MarginPurchaseBuy"],
            "margin_sell": row["MarginPurchaseSell"],
            "margin_balance": row["MarginPurchaseTodayBalance"],
            "short_buy": row["ShortSaleBuy"],
            "short_sell": row["ShortSaleSell"],
            "short_balance": row["ShortSaleTodayBalance"],
        }
        for row in _fetch_range(MARGIN_DATASET, symbol, start_date, end_date)
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


def fetch_stock_name(symbol: str) -> str:
    """回傳這支股票的中文簡稱，查不到就回傳空字串。2026-08-16發現：TaiwanStockPER(見上面
    fetch_valuations_for_range的說明)確實沒有名稱欄位，但TaiwanStockInfo這個不同的
    dataset有——可以在daily_update.add_symbol_to_watchlist查完官方名稱API(TWSE/TPEx)
    後當最後備援，尤其TPEx官方API(www.tpex.org.tw)常因為對方SSL憑證問題整組查不到
    (同一個問題已經讓margin/institutional/valuation三個TPEx資料源都改用FinMind，見
    daily_update.py開頭2026-08-13那段說明，名稱查詢當時漏了改)。這個dataset不接受
    日期範圍查詢，只回傳「目前」的名稱，但公司名稱本來就幾乎不會變，不需要指定日期。"""
    resp = requests.get(BASE_URL, params={"dataset": STOCK_INFO_DATASET, "data_id": symbol}, timeout=TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("msg") != "success":
        return ""
    rows = payload.get("data", [])
    return rows[0]["stock_name"] if rows else ""
