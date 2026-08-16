import pandas as pd

from stocks.indicators import rsi
from stocks.models import Direction, SignalEvent, Tier


class RSIStrategy:
    """RSI跌破30(超賣)進場、突破70(超買)出場。斷路器：不適用(非NOTIFIABLE_STRATEGIES)。"""

    name = "rsi"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        period = params.get("period", 14)
        oversold = params.get("oversold", 30)
        overbought = params.get("overbought", 70)

        r = rsi(bars["close"], period)
        prev = r.shift(1)
        enter_oversold = (prev >= oversold) & (r < oversold)
        enter_overbought = (prev <= overbought) & (r > overbought)

        events = [
            SignalEvent(symbol, self.name, Direction.BUY, bars["close"][t], t, f"RSI跌破{oversold}(超賣)")
            for t in bars.index[enter_oversold]
        ]
        events += [
            SignalEvent(symbol, self.name, Direction.SELL, bars["close"][t], t, f"RSI突破{overbought}(超買)")
            for t in bars.index[enter_overbought]
        ]
        return events
