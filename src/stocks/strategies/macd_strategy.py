import pandas as pd

from stocks.indicators import macd
from stocks.models import Direction, SignalEvent, Tier


class MACDStrategy:
    """MACD黃金/死亡交叉。斷路器：不適用(非NOTIFIABLE_STRATEGIES)。"""

    name = "macd"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        fast = params.get("fast", 12)
        slow = params.get("slow", 26)
        signal = params.get("signal", 9)

        macd_line, signal_line, _ = macd(bars["close"], fast, slow, signal)
        diff = macd_line - signal_line
        prev = diff.shift(1)
        bullish = (prev <= 0) & (diff > 0)
        bearish = (prev >= 0) & (diff < 0)

        events = [
            SignalEvent(symbol, self.name, Direction.BUY, bars["close"][t], t, "MACD黃金交叉")
            for t in bars.index[bullish]
        ]
        events += [
            SignalEvent(symbol, self.name, Direction.SELL, bars["close"][t], t, "MACD死亡交叉")
            for t in bars.index[bearish]
        ]
        return events
