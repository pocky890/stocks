import pandas as pd

from stocks.indicators import rolling_avg_volume
from stocks.models import Direction, SignalEvent, Tier


class VolumeAnomalyStrategy:
    name = "volume_anomaly"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        avg_period = params.get("avg_period", 20)
        multiplier = params.get("multiplier", 2)

        avg_vol = rolling_avg_volume(bars["volume"], avg_period)
        is_anomaly = bars["volume"] > (multiplier * avg_vol)
        price_up = bars["close"].diff() > 0
        price_down = bars["close"].diff() < 0

        buy_mask = is_anomaly & price_up
        sell_mask = is_anomaly & price_down

        events = [
            SignalEvent(symbol, self.name, Direction.BUY, bars["close"][t], t, f"爆量上漲(>{multiplier}倍均量)")
            for t in bars.index[buy_mask]
        ]
        events += [
            SignalEvent(symbol, self.name, Direction.SELL, bars["close"][t], t, f"爆量下跌(>{multiplier}倍均量)")
            for t in bars.index[sell_mask]
        ]
        return events
