import pandas as pd

from stocks.indicators import atr, rolling_avg_volume
from stocks.models import Direction, SignalEvent


class BreakoutStrategy:
    """收盤價創前20日新高(唐奇安通道上軌，不含當天，跟atr_breakout同樣用shift(1)避免
    look-ahead)且成交量放大(>1.5倍均量)才進場，代表突破有量能支撐，濾掉量縮的假突破。
    出場是收盤價跌破前10日最低，停損取「進場價-2倍ATR」跟「前10日最低」兩者中先跌破的
    那個。屬於強勢單邊行情用的策略，跟trend_following的差異是這裡看的是「創新高」而不是
    均線排列，進場反應更快但假訊號也可能更多。"""

    name = "breakout"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        high_lookback_days = params.get("high_lookback_days", 20)
        low_lookback_days = params.get("low_lookback_days", 10)
        volume_avg_period = params.get("volume_avg_period", 20)
        volume_multiplier = params.get("volume_multiplier", 1.5)
        atr_period = params.get("atr_period", 14)
        atr_multiplier = params.get("atr_multiplier", 2)
        stop_mode = params.get("stop_mode", "atr")  # "atr"(進場後固定不動) 或 "pct"(移動停損)，
        # 2026-08-15新增供實測比較用——pct模式會把停損改成會跟漲上移的移動停損，跟原本
        # 進場後就固定不動的ATR停損是不同行為，不是單純換算距離的公式而已。
        stop_pct = params.get("stop_pct", 0.15)

        close = bars["close"]
        donchian_upper = bars["high"].rolling(window=high_lookback_days).max().shift(1)
        donchian_lower = bars["low"].rolling(window=low_lookback_days).min().shift(1)
        avg_volume = rolling_avg_volume(bars["volume"], volume_avg_period)
        atr_value = atr(bars["high"], bars["low"], close, atr_period)

        entry_condition = (close > donchian_upper) & (bars["volume"] > volume_multiplier * avg_volume)
        prev_entry = entry_condition.shift(1).fillna(False).astype(bool)
        entry_edge = entry_condition & ~prev_entry

        def next_stop(c: float, t) -> float:
            if stop_mode == "pct":
                return c * (1 - stop_pct)
            return c - atr_multiplier * atr_value[t]

        stop_label = f"{stop_pct * 100:.0f}%移動停損" if stop_mode == "pct" else "停損"

        events: list[SignalEvent] = []
        in_position = False
        stop = None

        for t in bars.index:
            c = close[t]
            if in_position:
                reasons = []
                if not pd.isna(donchian_lower[t]) and c < donchian_lower[t]:
                    reasons.append(f"跌破前{low_lookback_days}日最低{donchian_lower[t]:.2f}")
                if c < stop:
                    reasons.append(f"跌破{stop_label}{stop:.2f}")
                if reasons:
                    events.append(SignalEvent(symbol, self.name, Direction.SELL, c, t, "、".join(reasons)))
                    in_position = False
                    stop = None
                elif stop_mode == "pct":
                    stop = max(stop, next_stop(c, t))
            elif entry_edge[t] and not pd.isna(donchian_upper[t]) and (stop_mode == "pct" or not pd.isna(atr_value[t])):
                stop = next_stop(c, t)
                events.append(
                    SignalEvent(
                        symbol,
                        self.name,
                        Direction.BUY,
                        c,
                        t,
                        f"創{high_lookback_days}日新高且量>{volume_multiplier}倍均量，{stop_label}{stop:.2f}",
                    )
                )
                in_position = True

        return events
