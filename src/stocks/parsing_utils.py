"""共用的證交所/櫃買中心資料解析小工具（數字字串轉換、民國年轉西元年）。"""


def to_number(raw, cast=int):
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    if text in ("", "-", "--"):
        return None
    try:
        return cast(text)
    except ValueError:
        return None


def roc_date_to_iso(roc_date: str) -> str:
    """'1150805' (民國年+MMDD) -> '2026-08-05'"""
    roc_year = int(roc_date[:3])
    month = roc_date[3:5]
    day = roc_date[5:7]
    return f"{roc_year + 1911}-{month}-{day}"
