import pandas as pd

from stocks.indicators import atr
from stocks.models import Direction, SignalEvent


class ATRBreakoutStrategy:
    """通用型自適應策略：收盤價創過去N日新高（唐奇安通道上軌）就進場，用N倍ATR設移動停損——
    不預設固定%停損，波動大的股票停損空間自動放寬、波動小的自動收窄，同一套邏輯可以套用在任何
    波動度的商品上。停損只進不退：每天先用「前一天算出的停損線」判斷是否出場，沒出場才用當天
    收盤價把停損線往上拉，避免用當天收盤價同時決定當天的出場與停損位置（look-ahead）。"""

    name = "atr_breakout"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        donchian_period = params.get("donchian_period", 20)
        atr_period = params.get("atr_period", 14)
        atr_multiplier = params.get("atr_multiplier", 2)

        close = bars["close"]
        donchian_upper = bars["high"].rolling(window=donchian_period).max().shift(1)
        atr_value = atr(bars["high"], bars["low"], close, atr_period)

        events: list[SignalEvent] = []
        in_position = False
        stop = None

        for t in bars.index:
            if pd.isna(donchian_upper[t]) or pd.isna(atr_value[t]):
                continue
            c = close[t]

            if in_position:
                if c < stop:
                    events.append(
                        SignalEvent(symbol, self.name, Direction.SELL, c, t, f"跌破ATR移動停損 {stop:.2f}")
                    )
                    in_position = False
                    stop = None
                else:
                    stop = max(stop, c - atr_multiplier * atr_value[t])
            elif c > donchian_upper[t]:
                stop = c - atr_multiplier * atr_value[t]
                events.append(
                    SignalEvent(symbol, self.name, Direction.BUY, c, t, f"創{donchian_period}日新高突破，ATR停損 {stop:.2f}")
                )
                in_position = True

        return events
