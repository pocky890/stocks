import numpy as np
import pandas as pd

from stocks.indicators import bollinger_bands, macd, rolling_avg_volume, rsi, sma, stochastic_kd
from stocks.models import Direction, SignalEvent


def _streak(net_series: pd.Series) -> tuple[pd.Series, pd.Series]:
    """回傳(sign, streak)兩個完整時間序列：sign是每天買超/賣超的正負號，streak是「當天
    為止」連續同號的天數。跟institutional_streak.py同一套算法，但這裡要整段序列(拿來
    跟其他條件在同一天做AND/OR判斷)，不是只挑「剛好達到門檻那一天」。"""
    sign = np.sign(net_series.fillna(0))
    group_id = (sign != sign.shift()).cumsum()
    streak = sign.groupby(group_id).cumcount() + 1
    return sign, streak


def compute_buy_condition(bars: pd.DataFrame, params: dict) -> tuple[pd.Series, dict]:
    """極簡買進公式(2026-08-06調整版)的完整布林序列(不只是「今天剛觸發」的edge，是
    「這一天算不算符合」的完整歷史)。邏輯改成「環境條件(先決狀態) AND 進場觸發(A或B)」，
    不是三個步驟都要當天疊在一起，環境條件成立的期間隨時被進場訊號打到都算數：
    環境條件(先決狀態，兩者都要成立)：
      1. 籌碼：近chip_lookback_days天(預設5天)裡，外資或投信買超的天數 >= chip_min_days(預設2天)
         (是「5天裡有幾天買」的滾動計數，不是要求連續)
      2. 趨勢：5日均線在20日均線之上，或MACD柱狀圖是正的，兩者任一即可
    進場觸發(當天事件，A或B任一即可)：
      A. 今日成交量 > volume_avg_period日均量(預設10天)*volume_multiplier(預設1.5倍)，且收盤價 >= 布林上軌
      B. 今日KD黃金交叉，且K值 < kd_k_threshold(預設40)
    回傳(condition, components)：condition是「環境 AND (A或B)」的布林序列，components是
    A/B各自的布林序列，用來組detail文字。BuyFormulaStrategy.evaluate()跟watchlist_view
    的「建議買進」清單共用這個函式，不要各自重算一份邏輯。"""
    chip_lookback_days = params.get("chip_lookback_days", 5)
    chip_min_days = params.get("chip_min_days", 2)
    fast = params.get("fast", 5)
    slow = params.get("slow", 20)
    macd_fast = params.get("macd_fast", 12)
    macd_slow = params.get("macd_slow", 26)
    macd_signal = params.get("macd_signal", 9)
    volume_avg_period = params.get("volume_avg_period", 10)
    volume_multiplier = params.get("volume_multiplier", 1.5)
    bollinger_period = params.get("bollinger_period", 20)
    bollinger_num_std = params.get("bollinger_num_std", 2)
    kd_rsv_period = params.get("kd_rsv_period", 9)
    kd_k_smooth = params.get("kd_k_smooth", 3)
    kd_d_smooth = params.get("kd_d_smooth", 3)
    kd_k_threshold = params.get("kd_k_threshold", 40)

    close = bars["close"]

    if "foreign_net" in bars.columns and "trust_net" in bars.columns:
        any_buy_day = (bars["foreign_net"] > 0) | (bars["trust_net"] > 0)
        chip_backed = any_buy_day.rolling(window=chip_lookback_days).sum() >= chip_min_days
    else:
        chip_backed = pd.Series(False, index=bars.index)

    ma_fast, ma_slow = sma(close, fast), sma(close, slow)
    _, _, histogram = macd(close, macd_fast, macd_slow, macd_signal)
    trend_up = (ma_fast > ma_slow) | (histogram > 0)
    environment = chip_backed & trend_up

    avg_vol = rolling_avg_volume(bars["volume"], volume_avg_period)
    volume_spike = bars["volume"] > (volume_multiplier * avg_vol)
    upper, _, _ = bollinger_bands(close, bollinger_period, bollinger_num_std)
    breakout = volume_spike & (close >= upper)

    k, d = stochastic_kd(bars["high"], bars["low"], close, kd_rsv_period, kd_k_smooth, kd_d_smooth)
    kd_diff = k - d
    kd_prev = kd_diff.shift(1)
    kd_golden = (kd_prev < 0) & (kd_diff > 0) & (k < kd_k_threshold)

    condition = environment & (breakout | kd_golden)
    return condition, {"breakout": breakout, "kd_golden": kd_golden}


class BuyFormulaStrategy:
    """使用者定義的極簡買進公式，「環境條件成立期間，隨時被進場訊號打到」就觸發
    (edge-triggered，條件從不成立變成成立的第一天發一次，不是每天都成立就重複發)。
    條件定義見compute_buy_condition()。只定義BUY方向，跟原始需求一致，不擅自加對稱的
    賣出條件。"""

    name = "buy_formula"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        close = bars["close"]
        condition, components = compute_buy_condition(bars, params)
        prev_condition = condition.shift(1).fillna(False).astype(bool)
        buy_edge = condition & ~prev_condition

        events = []
        for t in bars.index[buy_edge]:
            timing = [
                label
                for label, series in [("爆量突破布林上軌", components["breakout"]), ("KD黃金交叉", components["kd_golden"])]
                if series[t]
            ]
            detail = f"極簡買進公式成立({'、'.join(timing)})"
            events.append(SignalEvent(symbol, self.name, Direction.BUY, close[t], t, detail))
        return events


class SellFormulaStrategy:
    """使用者定義的極簡賣出公式(2026-08-06第二次調整版)，2條件OR，任一成立就觸發
    (edge-triggered，成立的第一天發一次警戒，不是每天符合就重複發)：
    1. 跌破5日均線，且(RSI突破80超買 或 三大法人連續賣超達chip_days天)
    2. 跌破10日均線(不管有沒有其他條件confirm，這條線本身就是最後的停損提示)
    只定義SELL方向，跟原始需求一致。上一版是「3組警訊符合2組」，這版改成明確的
    2條件OR(不是計數門檻)，使用者實測回測結果後直接指定這個新結構。"""

    name = "sell_formula"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        chip_days = params.get("chip_days", 3)
        rsi_period = params.get("rsi_period", 14)
        rsi_overbought = params.get("rsi_overbought", 80)
        fast = params.get("fast", 5)
        slow = params.get("slow", 10)

        close = bars["close"]

        rsi_value = rsi(close, rsi_period)
        overheated = rsi_value > rsi_overbought

        if "foreign_net" in bars.columns and "trust_net" in bars.columns:
            foreign_sign, foreign_streak = _streak(bars["foreign_net"])
            trust_sign, trust_streak = _streak(bars["trust_net"])
            institutional_selling = ((foreign_sign == -1) & (foreign_streak >= chip_days)) | (
                (trust_sign == -1) & (trust_streak >= chip_days)
            )
        else:
            institutional_selling = pd.Series(False, index=bars.index)

        below_fast_ma = close < sma(close, fast)
        below_slow_ma = close < sma(close, slow)

        warning_1 = below_fast_ma & (overheated | institutional_selling)
        condition = warning_1 | below_slow_ma
        prev_condition = condition.shift(1).fillna(False).astype(bool)
        sell_edge = condition & ~prev_condition

        events = []
        for t in bars.index[sell_edge]:
            triggered = []
            if below_fast_ma[t] and overheated[t]:
                triggered.append(f"跌破{fast}日均線+RSI超買")
            if below_fast_ma[t] and institutional_selling[t]:
                triggered.append(f"跌破{fast}日均線+法人連續賣超")
            if below_slow_ma[t]:
                triggered.append(f"跌破{slow}日均線")
            detail = f"極簡賣出公式({'、'.join(triggered)})"
            events.append(SignalEvent(symbol, self.name, Direction.SELL, close[t], t, detail))
        return events
