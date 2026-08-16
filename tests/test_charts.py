import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))

import pandas as pd

from charts import price_and_chip_chart


def _make_bars(dates: pd.DatetimeIndex) -> pd.DataFrame:
    n = len(dates)
    return pd.DataFrame(
        {"open": range(n), "high": range(n), "low": range(n), "close": range(n), "volume": [1000] * n},
        index=dates,
    ).astype(float)


def test_price_and_chip_chart_skips_missing_weekday_via_rangebreaks():
    """2026-08-16使用者要求驗證：未來如果平日真的休市(颱風假等)，bars_daily那天完全
    沒有資料(不是yfinance塞的假K棒)，圖表要能判斷出這是缺資料的假日、整批跳過不留
    空白——不是只靠週末的bounds=["sat","mon"]，平日的缺口要進rangebreaks的values。
    這裡模擬一個平日缺口(拿掉一個週三)，驗證它確實被抓進rangebreaks，plotly才會把
    這天完全壓縮掉，不會畫出空白或詭異的K棒。"""
    dates = pd.bdate_range("2026-08-03", "2026-08-14")
    holiday = pd.Timestamp("2026-08-05")
    dates = dates[dates != holiday]
    bars = _make_bars(dates)

    fig = price_and_chip_chart(bars, None)

    rangebreaks = fig.layout.xaxis.rangebreaks
    weekday_breaks = [rb for rb in rangebreaks if getattr(rb, "values", None)]
    assert len(weekday_breaks) == 1
    assert list(weekday_breaks[0].values) == [holiday]


def test_price_and_chip_chart_no_weekday_rangebreak_when_no_gaps():
    """完全沒有平日缺口時(每個交易日都有資料)，不該多加一個空的values斷點。"""
    dates = pd.bdate_range("2026-08-03", "2026-08-14")
    bars = _make_bars(dates)

    fig = price_and_chip_chart(bars, None)

    rangebreaks = fig.layout.xaxis.rangebreaks
    weekday_breaks = [rb for rb in rangebreaks if getattr(rb, "values", None)]
    assert weekday_breaks == []
