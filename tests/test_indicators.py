import pandas as pd
import pytest

from stocks.indicators import bollinger_bands, ema, macd, rolling_avg_volume, rsi, sma, stochastic_kd


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


def test_stochastic_kd_matches_hand_computed_values():
    # high=low=close simplifies RSV to (close-min)/(max-min)*100 over the window
    closes = pd.Series([10.0, 12.0, 11.0, 15.0, 14.0, 20.0])
    k, d = stochastic_kd(closes, closes, closes, rsv_period=3, k_smooth=2, d_smooth=2)

    assert k.iloc[:2].isna().all()
    assert k.iloc[2] == pytest.approx(50.0)  # first valid RSV seeds K
    assert k.iloc[3] == pytest.approx(75.0)  # 0.5*50 + 0.5*100
    assert k.iloc[5] == pytest.approx(87.5)
    assert d.iloc[2] == pytest.approx(50.0)
    assert d.iloc[3] == pytest.approx(62.5)  # 0.5*50 + 0.5*75
    assert d.iloc[5] == pytest.approx(78.125)
