import pandas as pd

from stocks.indicators import bollinger_bands
from stocks.models import Direction, SignalEvent, Tier


class BollingerStrategy:
    name = "bollinger"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        period = params.get("period", 20)
        num_std = params.get("num_std", 2)

        upper, _, lower = bollinger_bands(bars["close"], period, num_std)
        close = bars["close"]
        prev_close = close.shift(1)
        prev_upper = upper.shift(1)
        prev_lower = lower.shift(1)

        touch_upper = (prev_close <= prev_upper) & (close > upper)
        touch_lower = (prev_close >= prev_lower) & (close < lower)

        events = [
            SignalEvent(symbol, self.name, Direction.SELL, close[t], t, "觸及布林上軌")
            for t in bars.index[touch_upper]
        ]
        events += [
            SignalEvent(symbol, self.name, Direction.BUY, close[t], t, "觸及布林下軌")
            for t in bars.index[touch_lower]
        ]
        return events
