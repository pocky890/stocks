import pandas as pd

from stocks.models import Direction, SignalEvent, Tier


class PriceAlertStrategy:
    """No default params: target_price must be supplied per-symbol by the caller
    (sourced from the price_alerts table), unlike the other 6 strategies.
    斷路器：不適用(非NOTIFIABLE_STRATEGIES)。"""

    name = "price_alert"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        target = params.get("target_price")
        if target is None:
            return []

        close = bars["close"]
        prev = close.shift(1)
        cross_up = (prev <= target) & (close > target)
        cross_down = (prev >= target) & (close < target)

        events = [
            SignalEvent(symbol, self.name, Direction.BUY, close[t], t, f"突破設定價位{target}")
            for t in bars.index[cross_up]
        ]
        events += [
            SignalEvent(symbol, self.name, Direction.SELL, close[t], t, f"跌破設定價位{target}")
            for t in bars.index[cross_down]
        ]
        return events
