import pandas as pd
import pytest

from stocks.indicators import bollinger_bands, ema, macd, rolling_avg_volume, rsi, sma


def test_sma_basic():
    series = pd.Series([1, 2, 3, 4, 5])
    result = sma(series, period=3)
    assert result.iloc[:2].isna().all()
    assert result.iloc[2] == pytest.approx(2.0)
    assert result.iloc[3] == pytest.approx(3.0)
    assert result.iloc[4] == pytest.approx(4.0)


def test_rsi_pure_uptrend_hits_100():
    series = pd.Series(range(1, 20))  # strictly increasing => no losses at all
    result = rsi(series, period=14)
    assert result.iloc[-1] == pytest.approx(100.0)


def test_rsi_pure_downtrend_hits_0():
    series = pd.Series(range(20, 1, -1))  # strictly decreasing => no gains at all
    result = rsi(series, period=14)
    assert result.iloc[-1] == pytest.approx(0.0)


def test_ema_matches_hand_computed_values():
    series = pd.Series([1.0, 2.0, 3.0])
    result = ema(series, period=2)  # alpha = 2/3
    assert result.iloc[0] == pytest.approx(1.0)
    assert result.iloc[1] == pytest.approx(5 / 3)
    assert result.iloc[2] == pytest.approx(23 / 9)


def test_macd_positive_on_accelerating_uptrend():
    series = pd.Series(range(1, 60))
    macd_line, signal_line, histogram = macd(series, fast=12, slow=26, signal=9)
    assert macd_line.iloc[-1] > 0, "fast EMA should sit above slow EMA in a steady uptrend"


def test_bollinger_bands_hand_computed():
    series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    upper, middle, lower = bollinger_bands(series, period=3, num_std=2)
    assert middle.iloc[2] == pytest.approx(2.0)
    assert upper.iloc[2] == pytest.approx(4.0)
    assert lower.iloc[2] == pytest.approx(0.0)


def test_rolling_avg_volume_basic():
    volume = pd.Series([100, 200, 300, 400, 500])
    result = rolling_avg_volume(volume, period=3)
    assert result.iloc[2] == pytest.approx(200.0)
    assert result.iloc[4] == pytest.approx(400.0)
