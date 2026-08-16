import pandas as pd

from stocks.indicators import sma
from stocks.models import Direction, SignalEvent, Tier


class MAAlignmentStrategy:
    """BUY fires once when price is simultaneously above the 5/10/20-day MA (AND-edge).
    SELL fires independently for each MA the price drops below (up to 3 separate events).
    斷路器：不適用(非NOTIFIABLE_STRATEGIES)。"""

    name = "ma_alignment"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        fast = params.get("fast", 5)
        mid = params.get("mid", 10)
        slow = params.get("slow", 20)

        close = bars["close"]
        ma5, ma10, ma20 = sma(close, fast), sma(close, mid), sma(close, slow)

        above_all = (close > ma5) & (close > ma10) & (close > ma20)
        prev_above_all = above_all.shift(1).fillna(False).astype(bool)
        buy_edge = above_all & ~prev_above_all

        events = [
            SignalEvent(symbol, self.name, Direction.BUY, close[t], t, f"站上{fast}/{mid}/{slow}日均線")
            for t in bars.index[buy_edge]
        ]

        for label, ma in [(f"{fast}日線", ma5), (f"{mid}日線", ma10), (f"{slow}日線", ma20)]:
            above = close > ma
            prev_above = above.shift(1).fillna(False).astype(bool)
            broke = ~above & prev_above
            events += [
                SignalEvent(symbol, self.name, Direction.SELL, close[t], t, f"跌破{label}")
                for t in bars.index[broke]
            ]

        return events
