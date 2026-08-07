import pandas as pd

from stocks.indicators import atr, bollinger_bands, rsi, sma
from stocks.models import Direction, SignalEvent


class RSIMeanReversionStrategy:
    """短週期RSI(預設2日)出現極端低值(<10)、同時收盤價跌到布林通道下軌之外，是短線超賣訊號，
    賭的是均值回歸。出場是RSI回升到70以上(轉強)或收盤價站回20日均線(價格已回歸均值)，停損
    取「跌破前5日最低」跟「跌破進場價-2倍ATR」兩者中先發生的那個。屬於盤整行情用的短線
    策略，強烈單向趨勢下容易被巴來巴去，不像trend_following/breakout那樣追單邊。"""

    name = "rsi_mean_reversion"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        rsi_period = params.get("rsi_period", 2)
        rsi_oversold = params.get("rsi_oversold", 10)
        rsi_overbought = params.get("rsi_overbought", 70)
        bollinger_period = params.get("bollinger_period", 20)
        bollinger_num_std = params.get("bollinger_num_std", 2)
        ma_period = params.get("ma_period", 20)
        low_lookback_days = params.get("low_lookback_days", 5)
        atr_period = params.get("atr_period", 14)
        atr_multiplier = params.get("atr_multiplier", 2)

        close = bars["close"]
        rsi_value = rsi(close, rsi_period)
        _, _, lower_band = bollinger_bands(close, bollinger_period, bollinger_num_std)
        ma = sma(close, ma_period)
        # shift(1)：不含當天，跟atr_breakout的唐奇安通道一樣避免look-ahead(當天的低點
        # 本身不該拿來當「跌破前N日低點」的判斷基準)。
        rolling_low = bars["low"].rolling(window=low_lookback_days).min().shift(1)
        atr_value = atr(bars["high"], bars["low"], close, atr_period)

        entry_condition = (rsi_value < rsi_oversold) & (close < lower_band)
        prev_entry = entry_condition.shift(1).fillna(False).astype(bool)
        entry_edge = entry_condition & ~prev_entry

        events: list[SignalEvent] = []
        in_position = False
        stop = None

        for t in bars.index:
            c = close[t]
            if in_position:
                reasons = []
                if not pd.isna(rolling_low[t]) and c < rolling_low[t]:
                    reasons.append(f"跌破前{low_lookback_days}日最低{rolling_low[t]:.2f}")
                if c < stop:
                    reasons.append(f"跌破停損{stop:.2f}")
                if rsi_value[t] > rsi_overbought:
                    reasons.append(f"RSI({rsi_period})回升至{rsi_overbought}以上")
                if c > ma[t]:
                    reasons.append(f"收盤站回{ma_period}日均線")
                if reasons:
                    events.append(SignalEvent(symbol, self.name, Direction.SELL, c, t, "、".join(reasons)))
                    in_position = False
                    stop = None
            elif entry_edge[t] and not pd.isna(atr_value[t]):
                stop = c - atr_multiplier * atr_value[t]
                events.append(
                    SignalEvent(
                        symbol, self.name, Direction.BUY, c, t, f"RSI({rsi_period})<{rsi_oversold}且跌破布林下軌，停損{stop:.2f}"
                    )
                )
                in_position = True

        return events
