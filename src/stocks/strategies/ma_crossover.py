import pandas as pd

from stocks.indicators import sma
from stocks.models import Direction, SignalEvent, Tier


class MACrossoverStrategy:
    """均線交叉：5日均線黃金/死亡交叉20日均線。斷路器：不適用(非NOTIFIABLE_STRATEGIES)。"""

    name = "ma_crossover"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        fast = params.get("fast", 5)
        slow = params.get("slow", 20)

        diff = sma(bars["close"], fast) - sma(bars["close"], slow)
        prev = diff.shift(1)
        golden = (prev <= 0) & (diff > 0)
        death = (prev >= 0) & (diff < 0)

        events = [
            SignalEvent(symbol, self.name, Direction.BUY, bars["close"][t], t, "golden cross")
            for t in bars.index[golden]
        ]
        events += [
            SignalEvent(symbol, self.name, Direction.SELL, bars["close"][t], t, "death cross")
            for t in bars.index[death]
        ]
        return events
