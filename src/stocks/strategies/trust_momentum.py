import pandas as pd

from stocks.indicators import atr, rsi, sma
from stocks.models import Direction, SignalEvent


class TrustMomentumStrategy:
    """投信買超動能策略，跟chip_momentum同一套邏輯，主訊號換成投信(trust_net)買超。

    進場：近15日(cum_window_days)累積買超為正 + 近3日內至少2日買超(entry_mode="window10_3")
    + RSI(14)未超買(<70)
    出場：跌破15%移動停損(stop_mode="pct")，也支援ATR移動停損("atr")、分批停損("tiered_pct")

    進場是level-triggered(條件當天成立就觸發，不要求剛從False轉True)，停損出場後只要
    條件仍成立就能立刻重新進場(除非設定cooldown_days>0，研究參數，見下方)。

    斷路器：適用——全市場同產業≥60%股票跌破月線(20日均線)、且這支股票自己當下也跌破
    月線時，暫停新的BUY(SELL不受影響)。

    沒有trust_net欄位就直接跳過。"""

    name = "trust_momentum"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        if "trust_net" not in bars.columns:
            return []

        chip_window_days = params.get("chip_window_days", 5)
        chip_min_buy_days = params.get("chip_min_buy_days", 3)
        rsi_period = params.get("rsi_period", 14)
        rsi_overbought = params.get("rsi_overbought", 70)
        atr_period = params.get("atr_period", 14)
        atr_multiplier = params.get("atr_multiplier", 2.5)
        stop_mode = params.get("stop_mode", "pct")  # "atr"、"pct"(現行:固定15%移動停損) 或
        # "tiered_pct"(分批停損)
        stop_pct = params.get("stop_pct", 0.15)
        stop_pct_half = params.get("stop_pct_half", 0.08)
        stop_pct_full = params.get("stop_pct_full", 0.15)
        require_uptrend = params.get("require_uptrend", False)  # 額外要求收盤站上
        # trend_ma_period日均線，過濾大盤/個股趨勢已轉弱但投信仍在買的假訊號。預設False。
        trend_ma_period = params.get("trend_ma_period", 60)
        entry_mode = params.get("entry_mode", "default")  # "default"(近chip_window_days日內
        # 至少chip_min_buy_days天買超且淨額為正，單一視窗) 或 "window10_3"(現行:近
        # cum_window_days日累積淨額為正，且近recent_window_days日內至少recent_min_buy_days
        # 天買超，兩層視窗)
        cum_window_days = params.get("cum_window_days", 10)
        recent_window_days = params.get("recent_window_days", 3)
        recent_min_buy_days = params.get("recent_min_buy_days", 2)
        cooldown_days = params.get("cooldown_days", 0)  # 停損出場後N天內不重新進場，
        # 預設0(現行:level-triggered，條件仍成立就立刻重新進場)，防止投信左側攤平時
        # 連續吃好幾次停損

        close = bars["close"]
        trust_net = bars["trust_net"].fillna(0)
        if entry_mode == "window10_3":
            cum_positive = trust_net.rolling(cum_window_days).sum() > 0
            recent_buy_days = (trust_net > 0).rolling(recent_window_days).sum()
            trust_buy_streak = cum_positive & (recent_buy_days >= recent_min_buy_days)
        else:
            buy_days_in_window = (trust_net > 0).rolling(window=chip_window_days).sum()
            net_sum_in_window = trust_net.rolling(window=chip_window_days).sum()
            trust_buy_streak = (buy_days_in_window >= chip_min_buy_days) & (net_sum_in_window > 0)

        not_overbought = rsi(close, rsi_period) < rsi_overbought
        entry_condition = trust_buy_streak & not_overbought
        if require_uptrend:
            entry_condition = entry_condition & (close > sma(close, trend_ma_period))

        if stop_mode == "tiered_pct":
            events: list[SignalEvent] = []
            in_position = False
            half_sold = False
            stop_half = None
            stop_full = None
            cooldown_remaining = 0

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
                        cooldown_remaining = cooldown_days
                elif cooldown_remaining > 0:
                    cooldown_remaining -= 1
                elif entry_condition[t]:
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
                            f"投信近{chip_window_days}日{chip_min_buy_days}天以上買超(未超買)，"
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
        cooldown_remaining = 0

        for t in bars.index:
            c = close[t]
            if in_position:
                if c < stop:
                    events.append(SignalEvent(symbol, self.name, Direction.SELL, c, t, f"跌破{stop_label} {stop:.2f}"))
                    in_position = False
                    stop = None
                    cooldown_remaining = cooldown_days
                elif stop_mode == "pct" or not pd.isna(atr_value[t]):
                    stop = max(stop, next_stop(c, t))
            elif cooldown_remaining > 0:
                cooldown_remaining -= 1
            elif entry_condition[t] and (stop_mode == "pct" or not pd.isna(atr_value[t])):
                stop = next_stop(c, t)
                events.append(
                    SignalEvent(
                        symbol,
                        self.name,
                        Direction.BUY,
                        c,
                        t,
                        f"投信近{chip_window_days}日{chip_min_buy_days}天以上買超(未超買)，{stop_label} {stop:.2f}",
                    )
                )
                in_position = True

        return events
