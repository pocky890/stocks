"""證交所（TWSE，上市）免費公開資料（不需要金鑰、不需要開戶）。用來補三大法人/融資融券/
估值/除權息，跟Shioaji帳戶狀態完全無關。上櫃(TPEx)股票對應的資料在 tpex_client.py。"""
import time

import requests

from stocks.parsing_utils import roc_date_to_iso, to_number

TIMEOUT = 15
RETRIES = 3
RETRY_BACKOFF_SECONDS = 3


def _get_json(url: str, params: dict | None = None, retries: int = RETRIES) -> dict | list:
    """TWSE免費API常態性地會偶爾逾時或回空的body（非JSON），對逐日回補上百次呼叫來說
    幾乎必然遇到，重試幾次通常就過了，不是資料本身有問題。retries可調——像
    scripts/fetch_market_data.py那種本來就要跑很久的回補腳本用預設值多重試；但dashboard
    載入時check_and_update()是即時互動路徑，TWSE不穩時應該直接放棄改下次再抓，不該讓
    使用者等重試+逾時(retries=1相當於不重試，跟加重試機制之前的行為一樣)。"""
    last_error = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < retries - 1:
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise last_error


def fetch_institutional_flows_for_date(date_iso: str, retries: int = RETRIES) -> list[dict]:
    """三大法人買賣超日報，全市場一次回來，呼叫端自己篩選要的symbol。"""
    date_str = date_iso.replace("-", "")
    payload = _get_json(
        "https://www.twse.com.tw/rwd/zh/fund/T86",
        params={"date": date_str, "selectType": "ALL", "response": "json"},
        retries=retries,
    )
    if payload.get("stat") != "OK":
        return []

    rows = []
    for row in payload.get("data", []):
        foreign_net = (to_number(row[4]) or 0) + (to_number(row[7]) or 0)
        rows.append(
            {
                "symbol": row[0],
                "date": date_iso,
                "foreign_net": foreign_net,
                "trust_net": to_number(row[10]),
                "dealer_net": to_number(row[11]),
                "total_net": to_number(row[18]),
            }
        )
    return rows


def fetch_margin_balances_for_date(date_iso: str, retries: int = RETRIES) -> list[dict]:
    date_str = date_iso.replace("-", "")
    payload = _get_json(
        "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN",
        params={"date": date_str, "selectType": "ALL", "response": "json"},
        retries=retries,
    )
    if payload.get("stat") != "OK":
        return []

    tables = payload.get("tables", [])
    if len(tables) < 2:
        return []
    per_stock_table = tables[1]  # tables[0] is the market-wide summary, tables[1] is per-stock

    rows = []
    for row in per_stock_table.get("data", []):
        rows.append(
            {
                "symbol": row[0],
                "date": date_iso,
                "margin_buy": to_number(row[2]),
                "margin_sell": to_number(row[3]),
                "margin_balance": to_number(row[6]),
                "short_buy": to_number(row[8]),
                "short_sell": to_number(row[9]),
                "short_balance": to_number(row[12]),
            }
        )
    return rows


def fetch_valuations_for_date(date_iso: str, retries: int = RETRIES) -> list[dict]:
    date_str = date_iso.replace("-", "")
    payload = _get_json(
        "https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d",
        params={"date": date_str, "selectType": "ALL", "response": "json"},
        retries=retries,
    )
    if payload.get("stat") != "OK":
        return []

    rows = []
    for row in payload.get("data", []):
        rows.append(
            {
                "symbol": row[0],
                "name": row[1],
                "date": date_iso,
                "pe_ratio": to_number(row[5], cast=float),
                "dividend_yield": to_number(row[3], cast=float),
                "pb_ratio": to_number(row[6], cast=float),
            }
        )
    return rows


def fetch_company_directory(retries: int = RETRIES) -> list[dict]:
    """全部上市公司的代號/簡稱清單(不是逐日查詢，一次拿全部)——不需要知道代號、只知道
    中文名稱(例如「台積電」)也能查出對應代號，給daily_update.add_symbol_to_watchlist的
    名稱解析用。"公司簡稱"才是使用者平常講的名字(例如「台積電」)，"公司名稱"是完整法定
    登記名稱(例如「台灣積體電路製造股份有限公司」)，兩者不一樣，用簡稱才對得上symbols
    表裡existing的name欄位慣例。"""
    payload = _get_json("https://openapi.twse.com.tw/v1/opendata/t187ap03_L", retries=retries)
    return [{"symbol": row["公司代號"], "name": row["公司簡稱"].strip()} for row in payload]


def fetch_ex_dividend_schedule(retries: int = RETRIES) -> list[dict]:
    """上市股票除權息預告表：這是往前看的公告清單（不是逐日查詢），一次呼叫拿到所有排定中的除權息。"""
    payload = _get_json("https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL", retries=retries)

    rows = []
    for row in payload:
        rows.append(
            {
                "symbol": row.get("Code"),
                "ex_date": roc_date_to_iso(row["Date"]),
                "cash_dividend": to_number(row.get("CashDividend"), cast=float),
                "stock_dividend_ratio": row.get("StockDividendRatio") or None,
                "detail": row.get("Exdividend"),
            }
        )
    return rows
