import pandas as pd

from stocks.indicators import atr, rolling_avg_volume
from stocks.models import Direction, SignalEvent


class CapitulationReversalStrategy:
    """單日重挫+爆量(恐慌性賣壓出盡的典型特徵)，隔天如果不再破前一天低點、收盤又收在
    前一天收盤之上，視為止穩訊號進場——賭的是「該倒的都倒了」。跟bullish_divergence
    (抓跌勢動能逐漸減弱的過程)是不同角度：這支抓的是單一事件式的恐慌出清，訊號更即時
    (隔天就進場，不用等趨勢真正走弱)，但也更容易誤判成「還沒跌完」的假止穩(接刀子)。
    出場預設用固定15%移動停損(stop_mode="pct")，也支援ATR移動停損(stop_mode="atr")跟
    分批停損(stop_mode="tiered_pct")——實測比較結果跟改預設值的理由見bullish_divergence.py
    同一段註解(2026-08-15backtest_bottom_pickers.py三支策略一起測出來的結論)。"""

    name = "capitulation_reversal"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        drop_threshold_pct = params.get("drop_threshold_pct", -5.0)
        volume_multiplier = params.get("volume_multiplier", 2.0)
        avg_volume_period = params.get("avg_volume_period", 20)
        atr_period = params.get("atr_period", 14)
        atr_multiplier = params.get("atr_multiplier", 2.5)
        stop_mode = params.get("stop_mode", "pct")  # "atr"、"pct" 或 "tiered_pct"
        stop_pct = params.get("stop_pct", 0.15)
        stop_pct_half = params.get("stop_pct_half", 0.08)
        stop_pct_full = params.get("stop_pct_full", 0.15)

        close = bars["close"]
        low = bars["low"]
        avg_vol = rolling_avg_volume(bars["volume"], avg_volume_period)

        daily_return_pct = close.pct_change() * 100
        is_capitulation = (daily_return_pct <= drop_threshold_pct) & (bars["volume"] > volume_multiplier * avg_vol)

        # 用shift(1)看「前一天是不是爆量急殺日」，今天再確認止穩(不破前低+收盤收高)——
        # 隔天才進場，不是急殺當天就搶進場，避免當天盤中還在探底就先接刀。
        prev_is_capitulation = is_capitulation.shift(1).fillna(False)
        prev_close = close.shift(1)
        prev_low = low.shift(1)
        confirms_reversal = prev_is_capitulation & (close > prev_close) & (low >= prev_low)

        if stop_mode == "tiered_pct":
            events: list[SignalEvent] = []
            in_position = False
            half_sold = False
            stop_half = None
            stop_full = None

            for t in bars.index:
                c = close[t]
                if in_position:
                    if not half_sold:
                        stop_half = max(stop_half, c * (1 - stop_pct_half))
                    stop_full = max(stop_full, c * (1 - stop_pct_full))

                    if not half_sold and c < stop_half:
                        events.append(
                            SignalEvent(symbol, self.name, Direction.SELL, c, t, f"跌破{stop_pct_half * 100:.0f}%停損，賣出一半")
                        )
                        half_sold = True
                    if half_sold and c < stop_full:
                        events.append(
                            SignalEvent(symbol, self.name, Direction.SELL, c, t, f"跌破{stop_pct_full * 100:.0f}%停損，賣出剩餘一半")
                        )
                        in_position = False
                        half_sold = False
                        stop_half = None
                        stop_full = None
                elif confirms_reversal[t]:
                    stop_half = c * (1 - stop_pct_half)
                    stop_full = c * (1 - stop_pct_full)
                    in_position = True
                    half_sold = False
                    events.append(
                        SignalEvent(
                            symbol,
                            self.name,
                            Direction.BUY,
                            c,
                            t,
                            f"前日重挫{drop_threshold_pct:.0f}%+爆量{volume_multiplier:.0f}倍後隔日止穩，"
                            f"分批停損{stop_pct_half * 100:.0f}%/{stop_pct_full * 100:.0f}%",
                        )
                    )

            return events

        atr_value = atr(bars["high"], bars["low"], close, atr_period)

        def next_stop(c: float, t) -> float:
            if stop_mode == "pct":
                return c * (1 - stop_pct)
            return c - atr_multiplier * atr_value[t]

        stop_label = f"{stop_pct * 100:.0f}%移動停損" if stop_mode == "pct" else "ATR移動停損"

        events: list[SignalEvent] = []
        in_position = False
        stop = None

        for t in bars.index:
            c = close[t]
            if in_position:
                if c < stop:
                    events.append(SignalEvent(symbol, self.name, Direction.SELL, c, t, f"跌破{stop_label} {stop:.2f}"))
                    in_position = False
                    stop = None
                elif stop_mode == "pct" or not pd.isna(atr_value[t]):
                    stop = max(stop, next_stop(c, t))
            elif confirms_reversal[t] and (stop_mode == "pct" or not pd.isna(atr_value[t])):
                stop = next_stop(c, t)
                events.append(
                    SignalEvent(
                        symbol,
                        self.name,
                        Direction.BUY,
                        c,
                        t,
                        f"前日重挫{drop_threshold_pct:.0f}%+爆量{volume_multiplier:.0f}倍後隔日止穩，{stop_label} {stop:.2f}",
                    )
                )
                in_position = True

        return events
