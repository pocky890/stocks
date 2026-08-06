import pandas as pd

from stocks.indicators import stochastic_kd
from stocks.models import Direction, SignalEvent, Tier


class KDStrategy:
    name = "kd"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        rsv_period = params.get("rsv_period", 9)
        k_smooth = params.get("k_smooth", 3)
        d_smooth = params.get("d_smooth", 3)
        oversold = params.get("oversold", 20)
        overbought = params.get("overbought", 80)

        k, d = stochastic_kd(bars["high"], bars["low"], bars["close"], rsv_period, k_smooth, d_smooth)
        diff = k - d
        prev = diff.shift(1)
        # strict < / > (not <=/>=): D's ewm seeds from K's own first value, so diff is exactly 0
        # at that warmup row -- treating 0 as "coming from below/above" would flag a fake cross
        # on the very next bar regardless of real direction.
        golden = (prev < 0) & (diff > 0) & (k < oversold) & (d < oversold)
        death = (prev > 0) & (diff < 0) & (k > overbought) & (d > overbought)

        events = [
            SignalEvent(symbol, self.name, Direction.BUY, bars["close"][t], t, f"KD低檔黃金交叉 K:{k[t]:.0f} D:{d[t]:.0f}")
            for t in bars.index[golden]
        ]
        events += [
            SignalEvent(symbol, self.name, Direction.SELL, bars["close"][t], t, f"KD高檔死亡交叉 K:{k[t]:.0f} D:{d[t]:.0f}")
            for t in bars.index[death]
        ]
        return events
