import pandas as pd

from stocks.indicators import atr, rsi
from stocks.models import Direction, SignalEvent


class ChipMomentumStrategy:
    """外資買超動能策略。

    進場：外資連續5天(chip_streak_days)買超 + RSI(14)未超買(<70)
    出場：跌破15%移動停損(stop_mode="pct")，也支援2.5倍ATR移動停損("atr")、分批停損("tiered_pct")

    斷路器：適用——全市場同產業≥60%股票跌破月線(20日均線)、且這支股票自己當下也跌破
    月線時，暫停新的BUY(SELL不受影響)。

    沒有foreign_net欄位(bars沒join到institutional_flows)就直接跳過。"""

    name = "chip_momentum"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        if "foreign_net" not in bars.columns:
            return []

        chip_streak_days = params.get("chip_streak_days", 3)
        rsi_period = params.get("rsi_period", 14)
        rsi_overbought = params.get("rsi_overbought", 70)
        atr_period = params.get("atr_period", 14)
        atr_multiplier = params.get("atr_multiplier", 2.5)
        stop_mode = params.get("stop_mode", "pct")  # "atr"、"pct"(現行:固定15%移動停損) 或
        # "tiered_pct"(分批停損)
        stop_pct = params.get("stop_pct", 0.15)
        stop_pct_half = params.get("stop_pct_half", 0.08)
        stop_pct_full = params.get("stop_pct_full", 0.15)
        entry_mode = params.get("entry_mode", "streak")  # "streak"(現行:連續剛好
        # chip_streak_days天買超)、"window"(近cum_window_days日累積買超為正，且近
        # recent_window_days日內至少recent_min_buy_days天買超，比連續買超寬鬆) 或
        # "ratio"(近ratio_window_days日淨買超加總為正，且淨買超加總/總成交量加總>
        # ratio_threshold，用集中度取代天數頻率)
        cum_window_days = params.get("cum_window_days", 10)
        recent_window_days = params.get("recent_window_days", 3)
        recent_min_buy_days = params.get("recent_min_buy_days", 2)
        ratio_window_days = params.get("ratio_window_days", 5)
        ratio_threshold = params.get("ratio_threshold", 0.08)

        close = bars["close"]
        foreign_net = bars["foreign_net"].fillna(0)

        if entry_mode == "window":
            cum_positive = foreign_net.rolling(cum_window_days).sum() > 0
            recent_buy_days = (foreign_net > 0).rolling(recent_window_days).sum()
            foreign_buy_streak = cum_positive & (recent_buy_days >= recent_min_buy_days)
        elif entry_mode == "ratio":
            net_sum = foreign_net.rolling(ratio_window_days).sum()
            volume_sum = bars["volume"].rolling(ratio_window_days).sum()
            foreign_buy_streak = (net_sum > 0) & (net_sum / volume_sum > ratio_threshold)
        else:
            sign = foreign_net.apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
            group_id = (sign != sign.shift()).cumsum()
            streak = sign.groupby(group_id).cumcount() + 1
            foreign_buy_streak = (sign == 1) & (streak == chip_streak_days)

        not_overbought = rsi(close, rsi_period) < rsi_overbought
        entry_condition = foreign_buy_streak & not_overbought
        prev_entry = entry_condition.shift(1).fillna(False).astype(bool)
        entry_edge = entry_condition & ~prev_entry

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
                elif entry_edge[t]:
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
                            f"外資連{chip_streak_days}日買超(未超買)，分批停損{stop_pct_half * 100:.0f}%/{stop_pct_full * 100:.0f}%",
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
            elif entry_edge[t] and (stop_mode == "pct" or not pd.isna(atr_value[t])):
                stop = next_stop(c, t)
                events.append(
                    SignalEvent(symbol, self.name, Direction.BUY, c, t, f"外資連{chip_streak_days}日買超(未超買)，{stop_label} {stop:.2f}")
                )
                in_position = True

        return events
