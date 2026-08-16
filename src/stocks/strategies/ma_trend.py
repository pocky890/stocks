import pandas as pd

from stocks.indicators import sma
from stocks.models import Direction, SignalEvent


class MATrendStrategy:
    """收盤價同時站上快線(預設5日)跟慢線(預設20日)，且慢線本身也在上揚(斜率為正)，
    三個條件到齊才算多方排列確立，edge-triggered只在三個條件第一次同時成立那天觸發一次。
    只有BUY方向——使用者只描述了進場條件，沒有要求對稱的出場條件，不擅自發明。
    斷路器：不適用(非NOTIFIABLE_STRATEGIES)。"""

    name = "ma_trend"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        fast = params.get("fast", 5)
        slow = params.get("slow", 20)

        close = bars["close"]
        ma_fast = sma(close, fast)
        ma_slow = sma(close, slow)
        slow_rising = ma_slow.diff() > 0

        condition = (close > ma_fast) & (close > ma_slow) & slow_rising
        prev_condition = condition.shift(1).fillna(False).astype(bool)
        buy_edge = condition & ~prev_condition

        return [
            SignalEvent(symbol, self.name, Direction.BUY, close[t], t, f"站上{fast}/{slow}日均線且{slow}日線上揚")
            for t in bars.index[buy_edge]
        ]
