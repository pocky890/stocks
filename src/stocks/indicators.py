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


def weekly_trend_confirmed(daily_close: pd.Series, ma_period: int = 20, require_slope_up: bool = True) -> pd.Series:
    """多週期(日線進場+週線確認)濾網用：把日線收盤依週(週五收尾，W-FRI)聚合成週線，算
    週線MA，回傳對齊回日線index的布林序列。require_slope_up=True比對「週MA本身比上一週
    高(斜率向上)」；False比對「週收盤價是否站上週MA」。

    不需要額外shift(1)避免look-ahead：pandas resample的每個週線bar用「週五」當標籤，
    reindex(method="ffill")查某個日線日期時，只會找「標籤<=這個日期」的週線bar——當週
    還沒收完(還沒到週五)，這一週的標籤(週五)一定還沒到，天生查不到、只會退回上一個
    已經收完的週，不會不小心用到當週還沒發生的漲跌幫自己背書。"""
    weekly_close = daily_close.resample("W-FRI").last()
    weekly_ma = weekly_close.rolling(ma_period).mean()
    confirmed_weekly = weekly_ma.diff() > 0 if require_slope_up else weekly_close > weekly_ma
    return confirmed_weekly.reindex(daily_close.index, method="ffill").fillna(False).astype(bool)
