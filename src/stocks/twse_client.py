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
    """三大法人買賣超日報，全市場一次回來，呼叫端自己篩選要的symbol。

    2026-08-14回測拉長到10年時發現：TWSE在2017~2018年之間把「外資」拆成「外陸資(不含
    外資自營商)」+「外資自營商」兩個欄位，這之後所有欄位的位置都往後移了3格——硬編
    row[4]/row[7]/row[10]/row[11]/row[18]這種寫法碰到2018年以前的舊格式資料會直接
    IndexError(舊格式只有16欄，新格式19欄)。改成照fields裡的欄位名稱查值，不管欄位
    在哪個位置都找得到，同時處理新舊兩種「外資」欄位形狀。"""
    date_str = date_iso.replace("-", "")
    payload = _get_json(
        "https://www.twse.com.tw/rwd/zh/fund/T86",
        params={"date": date_str, "selectType": "ALL", "response": "json"},
        retries=retries,
    )
    if payload.get("stat") != "OK":
        return []

    idx = {name: i for i, name in enumerate(payload.get("fields", []))}
    has_foreign_split = "外資自營商買賣超股數" in idx  # 2018年之後的新格式才有這欄

    rows = []
    for row in payload.get("data", []):
        if has_foreign_split:
            foreign_net = (to_number(row[idx["外陸資買賣超股數(不含外資自營商)"]]) or 0) + (
                to_number(row[idx["外資自營商買賣超股數"]]) or 0
            )
        else:
            foreign_net = to_number(row[idx["外資買賣超股數"]]) or 0
        rows.append(
            {
                "symbol": row[0],
                "date": date_iso,
                "foreign_net": foreign_net,
                "trust_net": to_number(row[idx["投信買賣超股數"]]),
                "dealer_net": to_number(row[idx["自營商買賣超股數"]]),
                "total_net": to_number(row[idx["三大法人買賣超股數"]]),
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
    """2018年之前的舊格式只有[代號,名稱,本益比,殖利率,股價淨值比]5欄，2018年之後多了
    「收盤價」跟「股利年度」2欄，本益比/股價淨值比的位置因此往後移了——一樣改成照欄位
    名稱查值(見fetch_institutional_flows_for_date同樣的理由)，不是硬編位置。"""
    date_str = date_iso.replace("-", "")
    payload = _get_json(
        "https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d",
        params={"date": date_str, "selectType": "ALL", "response": "json"},
        retries=retries,
    )
    if payload.get("stat") != "OK":
        return []

    idx = {name: i for i, name in enumerate(payload.get("fields", []))}

    rows = []
    for row in payload.get("data", []):
        rows.append(
            {
                "symbol": row[0],
                "name": row[1],
                "date": date_iso,
                "pe_ratio": to_number(row[idx["本益比"]], cast=float),
                "dividend_yield": to_number(row[idx["殖利率(%)"]], cast=float),
                "pb_ratio": to_number(row[idx["股價淨值比"]], cast=float),
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
