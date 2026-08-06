import pandas as pd

from stocks.yfinance_client import _bars_from_dataframe


def test_bars_from_dataframe_skips_rows_with_nan_ohlc():
    """yfinance有時對還在成交中的當天回傳缺值OHLC(但Volume可能已經有部分值)，
    不是真正收盤的K棒——插進bars_daily會讓那天的收盤價變成NULL，污染所有策略計算。"""
    index = pd.to_datetime(["2026-08-05", "2026-08-06"])
    df = pd.DataFrame(
        {
            "Open": [100.0, None],
            "High": [110.0, None],
            "Low": [90.0, None],
            "Close": [105.0, None],
            "Volume": [1000, 500],
        },
        index=index,
    )

    bars = _bars_from_dataframe("2330", df)

    assert len(bars) == 1
    assert bars[0].ts == index[0].to_pydatetime()
    assert bars[0].close == 105.0
