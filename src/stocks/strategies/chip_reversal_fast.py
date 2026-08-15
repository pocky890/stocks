import pandas as pd

from stocks.indicators import atr, macd, stochastic_kd
from stocks.models import Direction, SignalEvent


class ChipReversalFastStrategy:
    """跟trust_momentum(近5日視窗累積確認買超動能)用同一個籌碼欄位(trust_net)，但邏輯
    方向相反：這支抓的是「連續N天賣超之後，第一天轉買超」就立刻進場，不等視窗累積確認，
    目的是搶在trust_momentum那種落後型確認之前卡位(trust_momentum常常等確認完，股價
    已經漲一段)。代價是誤判成本更高——連續賣超中間偶爾一天翻正很可能只是雜訊，隔天可能
    繼續賣。跟trust_momentum直接對照可以看出「快但容易被巴」vs「慢但確認度高」在同一組
    籌碼資料上的實際取捨。出場預設用固定15%移動停損(stop_mode="pct")，也支援ATR移動
    停損(stop_mode="atr")跟分批停損(stop_mode="tiered_pct")——實測比較結果跟改預設值
    的理由見bullish_divergence.py同一段註解(2026-08-15 backtest_bottom_pickers.py
    三支策略一起測出來的結論)。"""

    name = "chip_reversal_fast"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        if "trust_net" not in bars.columns:
            return []

        sell_streak_days = params.get("sell_streak_days", 3)
        atr_period = params.get("atr_period", 14)
        atr_multiplier = params.get("atr_multiplier", 2.5)
        stop_mode = params.get("stop_mode", "pct")  # "atr"、"pct" 或 "tiered_pct"
        stop_pct = params.get("stop_pct", 0.15)
        stop_pct_half = params.get("stop_pct_half", 0.08)
        stop_pct_full = params.get("stop_pct_full", 0.15)
        confirm_next_day = params.get("confirm_next_day", False)  # 2026-08-15研究中：
        # 跟bullish_divergence.py同一段註解——2026年7月系統性重挫期間這支策略進場後平均
        # 還要再跌一段才真正落底，借capitulation_reversal「隔天不再破前低+收盤收高才進場」
        # 的確認邏輯來試。預設False不影響既有行為。
        require_macd_turn = params.get("require_macd_turn", False)  # 額外要求MACD柱狀圖
        # 比前一天回升，跟bullish_divergence.py同一段理由。
        require_kd_bullish = params.get("require_kd_bullish", False)  # 額外要求K>D(偏多)。

        close = bars["close"]
        trust_net = bars["trust_net"].fillna(0)
        sign = trust_net.apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
        group_id = (sign != sign.shift()).cumsum()
        streak = sign.groupby(group_id).cumcount() + 1
        prev_sign = sign.shift(1)
        prev_streak = streak.shift(1)

        # 前一天是「連續>=sell_streak_days天賣超」的streak尾端，今天符號轉正——今天就是
        # 轉買超的第一天，跟trust_momentum等5日視窗累積轉正不同，這裡不等累積，看到就進場。
        just_reversed = (prev_sign == -1) & (prev_streak >= sell_streak_days) & (sign == 1)

        if require_macd_turn:
            _, _, histogram = macd(close)
            just_reversed = just_reversed & (histogram > histogram.shift(1))
        if require_kd_bullish:
            k, d = stochastic_kd(bars["high"], bars["low"], close)
            just_reversed = just_reversed & (k > d)

        if confirm_next_day:
            raw_signal = just_reversed.shift(1).fillna(False).astype(bool)
            just_reversed = raw_signal & (close > close.shift(1)) & (bars["low"] >= bars["low"].shift(1))

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
                elif just_reversed[t]:
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
                            f"投信連{sell_streak_days}日賣超後首日轉買超，"
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
            elif just_reversed[t] and (stop_mode == "pct" or not pd.isna(atr_value[t])):
                stop = next_stop(c, t)
                events.append(
                    SignalEvent(
                        symbol,
                        self.name,
                        Direction.BUY,
                        c,
                        t,
                        f"投信連{sell_streak_days}日賣超後首日轉買超，{stop_label} {stop:.2f}",
                    )
                )
                in_position = True

        return events
