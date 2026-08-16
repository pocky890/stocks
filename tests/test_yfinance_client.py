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


def test_bars_from_dataframe_skips_zero_volume_holiday_placeholder_rows():
    """2026-08-16發現：市場真的休市那天，yfinance有時會回傳open=high=low=close(通常是
    前一天收盤價)+volume=0的假K棒，不是真的有成交——這種列不該進bars_daily，不然
    dashboard的K線圖會多畫一根詭異的平盤零成交量K棒，backtest的滾動窗口計算也會被污染。"""
    index = pd.to_datetime(["2026-07-09", "2026-07-10", "2026-07-13"])
    df = pd.DataFrame(
        {
            "Open": [2190.0, 2220.0, 2275.0],
            "High": [2280.0, 2220.0, 2275.0],
            "Low": [2165.0, 2220.0, 2155.0],
            "Close": [2220.0, 2220.0, 2170.0],
            "Volume": [3707799, 0, 2703385],
        },
        index=index,
    )

    bars = _bars_from_dataframe("8299", df)

    assert [b.ts for b in bars] == [index[0].to_pydatetime(), index[2].to_pydatetime()]
