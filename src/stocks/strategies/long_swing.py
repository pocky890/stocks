import pandas as pd

from stocks.indicators import atr, rsi, sma
from stocks.models import Direction, SignalEvent


class LongSwingStrategy:
    """中長波段策略。

    進場：
    - Regime：60日均線>120日均線（多頭排列）才考慮進場
    - 首次進場：站上60日均線 + 外資與投信合計近20日買超為正 + RSI(14)未超買(<75)
    - 同一段regime裡的重新進場（regime沒斷過，即MA60未跌破MA120之前）：
      - 站回20日均線，且60日均線近5天仍上揚(斜率>0) → 不用重新確認法人/RSI即可進場
      - 60日均線走平/下彎時 → 退回要求完整條件(法人買超+RSI未超買)才能重進

    出場：
    - 收盤連續3天跌破60日均線，或
    - 跌破3.5倍20日ATR移動停損(stop_mode="atr")，也支援固定百分比移動停損("pct")

    斷路器：適用——全市場同產業≥60%股票跌破月線(20日均線)、且這支股票自己當下也跌破
    月線時，暫停新的BUY(SELL不受影響)。

    沒有foreign_net/trust_net任一欄位(bars沒join到institutional_flows)就直接跳過。"""

    name = "long_swing"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        if "foreign_net" not in bars.columns and "trust_net" not in bars.columns:
            return []

        trend_fast = params.get("trend_fast", 60)
        trend_slow = params.get("trend_slow", 120)
        atr_period = params.get("atr_period", 20)
        atr_multiplier = params.get("atr_multiplier", 3.5)
        chip_lookback_days = params.get("chip_lookback_days", 20)
        exit_confirm_days = params.get("exit_confirm_days", 3)
        rsi_overbought = params.get("rsi_overbought", 75)
        reentry_ma_period = params.get("reentry_ma_period", 20)
        slope_lookback = params.get("slope_lookback", 5)
        stop_mode = params.get("stop_mode", "atr")  # "atr"(現行:3.5倍ATR移動停損) 或
        # "pct"(固定百分比移動停損)
        stop_pct = params.get("stop_pct", 0.15)

        close = bars["close"]
        ma_fast = sma(close, trend_fast)
        ma_slow = sma(close, trend_slow)
        ma_reentry = sma(close, reentry_ma_period)
        atr_value = atr(bars["high"], bars["low"], close, atr_period)

        foreign_net = bars.get("foreign_net", pd.Series(0, index=bars.index)).fillna(0)
        trust_net = bars.get("trust_net", pd.Series(0, index=bars.index)).fillna(0)
        chip_support = (foreign_net + trust_net).rolling(chip_lookback_days).sum() > 0
        not_overbought = rsi(close, 14) < rsi_overbought
        trend_strong = ma_fast.diff(slope_lookback) > 0

        regime_active = ma_fast > ma_slow
        price_above_fast = close > ma_fast
        price_above_reentry = close > ma_reentry

        below_fast = close < ma_fast
        group_id = (below_fast != below_fast.shift()).cumsum()
        below_streak = below_fast.groupby(group_id).cumcount() + 1
        exit_confirmed = below_fast & (below_streak >= exit_confirm_days)

        def next_stop(c: float, t) -> float:
            if stop_mode == "pct":
                return c * (1 - stop_pct)
            return c - atr_multiplier * atr_value[t]

        stop_label = f"{stop_pct * 100:.0f}%移動停損" if stop_mode == "pct" else f"{atr_multiplier}倍ATR停損"

        events: list[SignalEvent] = []
        in_position = False
        stop = None
        had_entry_this_regime = False

        for t in bars.index:
            c = close[t]
            if pd.isna(ma_slow[t]) or (stop_mode == "atr" and pd.isna(atr_value[t])) or pd.isna(trend_strong[t]):
                continue
            if not regime_active[t]:
                had_entry_this_regime = False

            if in_position:
                exit_stop = c < stop
                if exit_confirmed[t] or exit_stop:
                    reasons = []
                    if exit_confirmed[t]:
                        reasons.append(f"連續{exit_confirm_days}天跌破{trend_fast}日均線")
                    if exit_stop:
                        reasons.append(f"跌破{stop_label} {stop:.2f}")
                    events.append(SignalEvent(symbol, self.name, Direction.SELL, c, t, "、".join(reasons)))
                    in_position = False
                    stop = None
                else:
                    stop = max(stop, next_stop(c, t))
            elif regime_active[t] and price_above_fast[t]:
                if not had_entry_this_regime:
                    if chip_support[t] and not_overbought[t]:
                        stop = next_stop(c, t)
                        in_position = True
                        had_entry_this_regime = True
                        events.append(
                            SignalEvent(
                                symbol,
                                self.name,
                                Direction.BUY,
                                c,
                                t,
                                f"首次進場：{trend_fast}日>{trend_slow}日均線多頭排列，法人近{chip_lookback_days}日買超為正，RSI未超買",
                            )
                        )
                elif price_above_reentry[t] and trend_strong[t]:
                    stop = next_stop(c, t)
                    in_position = True
                    events.append(
                        SignalEvent(
                            symbol,
                            self.name,
                            Direction.BUY,
                            c,
                            t,
                            f"同趨勢重新進場：站回{reentry_ma_period}日均線且{trend_fast}日均線仍上揚",
                        )
                    )
                elif price_above_reentry[t] and chip_support[t] and not_overbought[t]:
                    stop = next_stop(c, t)
                    in_position = True
                    events.append(
                        SignalEvent(
                            symbol,
                            self.name,
                            Direction.BUY,
                            c,
                            t,
                            f"{trend_fast}日均線走平但法人+RSI條件通過重新進場",
                        )
                    )

        return events
