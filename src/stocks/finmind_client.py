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

FOREIGN_NAMES = {"Foreign_Investor", "Foreign_Dealer_Self"}
TRUST_NAMES = {"Investment_Trust"}
DEALER_NAMES = {"Dealer_self", "Dealer_Hedging"}


def fetch_institutional_flows_for_range(symbol: str, start_date: str, end_date: str) -> list[dict]:
    """回傳[{symbol, date, foreign_net, trust_net, dealer_net, total_net}, ...]，
    日期照升序排列，一次涵蓋start_date~end_date整段範圍。"""
    resp = requests.get(
        BASE_URL,
        params={"dataset": DATASET, "data_id": symbol, "start_date": start_date, "end_date": end_date},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("msg") != "success":
        return []

    by_date: dict[str, dict[str, int]] = {}
    for row in payload.get("data", []):
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
