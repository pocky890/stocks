"""證交所免費公開資料（不需要金鑰、不需要開戶）。用來補三大法人/融資融券/估值/除權息，
跟Shioaji帳戶狀態完全無關。"""
import requests

TIMEOUT = 15


def _to_number(raw, cast=int):
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    if text in ("", "-", "--"):
        return None
    try:
        return cast(text)
    except ValueError:
        return None


def _roc_date_to_iso(roc_date: str) -> str:
    """'1150805' (ROC year+MMDD) -> '2026-08-05'"""
    roc_year = int(roc_date[:3])
    month = roc_date[3:5]
    day = roc_date[5:7]
    return f"{roc_year + 1911}-{month}-{day}"


def fetch_institutional_flows_for_date(date_iso: str) -> list[dict]:
    """三大法人買賣超日報，全市場一次回來，呼叫端自己篩選要的symbol。"""
    date_str = date_iso.replace("-", "")
    resp = requests.get(
        "https://www.twse.com.tw/rwd/zh/fund/T86",
        params={"date": date_str, "selectType": "ALL", "response": "json"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("stat") != "OK":
        return []

    rows = []
    for row in payload.get("data", []):
        foreign_net = (_to_number(row[4]) or 0) + (_to_number(row[7]) or 0)
        rows.append(
            {
                "symbol": row[0],
                "date": date_iso,
                "foreign_net": foreign_net,
                "trust_net": _to_number(row[10]),
                "dealer_net": _to_number(row[11]),
                "total_net": _to_number(row[18]),
            }
        )
    return rows


def fetch_margin_balances_for_date(date_iso: str) -> list[dict]:
    date_str = date_iso.replace("-", "")
    resp = requests.get(
        "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN",
        params={"date": date_str, "selectType": "ALL", "response": "json"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
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
                "margin_buy": _to_number(row[2]),
                "margin_sell": _to_number(row[3]),
                "margin_balance": _to_number(row[6]),
                "short_buy": _to_number(row[8]),
                "short_sell": _to_number(row[9]),
                "short_balance": _to_number(row[12]),
            }
        )
    return rows


def fetch_valuations_for_date(date_iso: str) -> list[dict]:
    date_str = date_iso.replace("-", "")
    resp = requests.get(
        "https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d",
        params={"date": date_str, "selectType": "ALL", "response": "json"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("stat") != "OK":
        return []

    rows = []
    for row in payload.get("data", []):
        rows.append(
            {
                "symbol": row[0],
                "date": date_iso,
                "pe_ratio": _to_number(row[5], cast=float),
                "dividend_yield": _to_number(row[3], cast=float),
                "pb_ratio": _to_number(row[6], cast=float),
            }
        )
    return rows


def fetch_ex_dividend_schedule() -> list[dict]:
    """上市股票除權息預告表：這是往前看的公告清單（不是逐日查詢），一次呼叫拿到所有排定中的除權息。"""
    resp = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL", timeout=TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()

    rows = []
    for row in payload:
        rows.append(
            {
                "symbol": row.get("Code"),
                "ex_date": _roc_date_to_iso(row["Date"]),
                "cash_dividend": _to_number(row.get("CashDividend"), cast=float),
                "stock_dividend_ratio": row.get("StockDividendRatio") or None,
                "detail": row.get("Exdividend"),
            }
        )
    return rows
