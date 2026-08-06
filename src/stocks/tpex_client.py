"""櫃買中心（TPEx，上櫃）免費公開資料。跟twse_client.py對應同樣4種資料，但這幾個endpoint
不支援指定日期查詢，每次呼叫只回傳「最新一個交易日」的資料 -- 沒辦法backfill歷史，
只能從開始抓的那天起慢慢累積。回傳的dict裡的"date"是從回應本身的Date欄位轉換來的，
不是呼叫時的系統時間，避免週末/收盤前呼叫時日期誤判。"""
import requests

from stocks.parsing_utils import roc_date_to_iso, to_number

TIMEOUT = 15


def fetch_institutional_flows_latest() -> list[dict]:
    resp = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading", timeout=TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()

    rows = []
    for row in payload:
        rows.append(
            {
                "symbol": row["SecuritiesCompanyCode"],
                "name": row["CompanyName"],
                "date": roc_date_to_iso(row["Date"]),
                # NB: TPEx's JSON key for this field really does have a stray space in the
                # middle ("...Include MainlandAreaInvestors...") -- confirmed against the live
                # API; the no-space variant only exists for -TotalBuy/-TotalSell, not -Difference.
                "foreign_net": to_number(row.get("ForeignInvestorsInclude MainlandAreaInvestors-Difference")),
                "trust_net": to_number(row.get("SecuritiesInvestmentTrustCompanies-Difference")),
                "dealer_net": to_number(row.get("Dealers-Difference")),
                "total_net": to_number(row.get("TotalDifference")),
            }
        )
    return rows


def fetch_margin_balances_latest() -> list[dict]:
    resp = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance", timeout=TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()

    rows = []
    for row in payload:
        rows.append(
            {
                "symbol": row["SecuritiesCompanyCode"],
                "date": roc_date_to_iso(row["Date"]),
                "margin_buy": to_number(row.get("MarginPurchase")),
                "margin_sell": to_number(row.get("MarginSales")),
                "margin_balance": to_number(row.get("MarginPurchaseBalance")),
                "short_buy": to_number(row.get("ShortConvering")),
                "short_sell": to_number(row.get("ShortSale")),
                "short_balance": to_number(row.get("ShortSaleBalance")),
            }
        )
    return rows


def fetch_valuations_latest() -> list[dict]:
    resp = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis", timeout=TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()

    rows = []
    for row in payload:
        rows.append(
            {
                "symbol": row["SecuritiesCompanyCode"],
                "name": row["CompanyName"],
                "date": roc_date_to_iso(row["Date"]),
                "pe_ratio": to_number(row.get("PriceEarningRatio"), cast=float),
                "dividend_yield": to_number(row.get("YieldRatio"), cast=float),
                "pb_ratio": to_number(row.get("PriceBookRatio"), cast=float),
            }
        )
    return rows


def fetch_ex_dividend_schedule() -> list[dict]:
    """上櫃股票除權息預告表：跟twse_client版本一樣是往前看的公告清單，不受「只有最新一天」限制。"""
    resp = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_exright_prepost", timeout=TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()

    rows = []
    for row in payload:
        rows.append(
            {
                "symbol": row.get("SecuritiesCompanyCode"),
                "ex_date": roc_date_to_iso(row["ExRrightsExDividendDate"]),
                "cash_dividend": to_number(row.get("CashDividend"), cast=float),
                "stock_dividend_ratio": row.get("StockDividendRatio") or None,
                "detail": row.get("ExRrightsExDividend"),
            }
        )
    return rows
