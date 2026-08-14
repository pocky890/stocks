import pandas as pd

from stocks.indicators import atr, rolling_avg_volume, sma
from stocks.models import Direction, SignalEvent


class TrendFollowingStrategy:
    """20日均線站上60日均線、收盤價站上20日均線、且成交量放大(>20日均量)才進場——三個條件
    都是「多頭排列+量能確認」，避免在盤整量縮時搶進。停損是進場當天的收盤價減2倍ATR，
    固定不動(不像atr_breakout那樣往上移動)；出場則是收盤跌破20日均線、或20日均線本身
    跌破60日均線(代表多頭排列瓦解)，兩者任一發生就出場。"""

    name = "trend_following"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        fast = params.get("fast", 20)
        slow = params.get("slow", 60)
        volume_avg_period = params.get("volume_avg_period", 20)
        atr_period = params.get("atr_period", 14)
        atr_multiplier = params.get("atr_multiplier", 2)
        stop_mode = params.get("stop_mode", "atr")  # "atr"(進場後固定不動) 或 "pct"(移動停損)，
        # 2026-08-15新增供實測比較用，取捨說明同breakout.py。
        stop_pct = params.get("stop_pct", 0.15)

        close = bars["close"]
        ma_fast = sma(close, fast)
        ma_slow = sma(close, slow)
        avg_volume = rolling_avg_volume(bars["volume"], volume_avg_period)
        atr_value = atr(bars["high"], bars["low"], close, atr_period)

        entry_condition = (ma_fast > ma_slow) & (close > ma_fast) & (bars["volume"] > avg_volume)
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
                if c < stop:
                    reasons.append(f"跌破{stop_label}{stop:.2f}")
                if c < ma_fast[t]:
                    reasons.append(f"跌破{fast}日均線")
                if ma_fast[t] < ma_slow[t]:
                    reasons.append(f"{fast}日均線跌破{slow}日均線")
                if reasons:
                    events.append(SignalEvent(symbol, self.name, Direction.SELL, c, t, "、".join(reasons)))
                    in_position = False
                    stop = None
                elif stop_mode == "pct":
                    stop = max(stop, next_stop(c, t))
            elif entry_edge[t] and (stop_mode == "pct" or not pd.isna(atr_value[t])):
                stop = next_stop(c, t)
                events.append(
                    SignalEvent(symbol, self.name, Direction.BUY, c, t, f"站上{fast}日均線且{fast}>{slow}日均線+爆量，{stop_label}{stop:.2f}")
                )
                in_position = True

        return events
