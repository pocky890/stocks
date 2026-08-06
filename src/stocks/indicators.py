import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger_bands(series: pd.Series, period: int = 20, num_std: float = 2):
    middle = sma(series, period)
    std = series.rolling(window=period).std()
    upper = middle + num_std * std
    lower = middle - num_std * std
    return upper, middle, lower


def rolling_avg_volume(volume: pd.Series, period: int = 20) -> pd.Series:
    return volume.rolling(window=period).mean()


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """True range取三者最大：當日high-low、跟前一日收盤的high/low差距（涵蓋跳空缺口），
    再取period日簡單移動平均（不是Wilder平滑，跟本檔案其他指標的簡單風格一致）。"""
    prev_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return true_range.rolling(window=period).mean()


def stochastic_kd(high: pd.Series, low: pd.Series, close: pd.Series, rsv_period: int = 9, k_smooth: int = 3, d_smooth: int = 3):
    """台股慣用KD：K = 前值*(k_smooth-1)/k_smooth + RSV/k_smooth，等同alpha=1/k_smooth的ewm。"""
    lowest_low = low.rolling(window=rsv_period).min()
    highest_high = high.rolling(window=rsv_period).max()
    rsv = (close - lowest_low) / (highest_high - lowest_low) * 100
    k = rsv.ewm(alpha=1 / k_smooth, adjust=False).mean()
    d = k.ewm(alpha=1 / d_smooth, adjust=False).mean()
    return k, d
