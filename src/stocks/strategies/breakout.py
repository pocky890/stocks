import pandas as pd

from stocks.indicators import atr, rolling_avg_volume, weekly_trend_confirmed
from stocks.models import Direction, SignalEvent


class BreakoutStrategy:
    """突破策略。

    進場：收盤價創前20日新高(唐奇安通道上軌) + 成交量>1.5倍均量 + 週線趨勢確認
    (require_weekly_trend：週MA20斜率向上)

    出場：收盤價跌破前10日最低，或跌破「進場價-2倍14日ATR」停損(stop_mode="atr"，進場後
    固定不動)，兩者先發生的為準。也支援stop_mode="pct"(移動停損)。

    斷路器：適用——全市場同產業≥60%股票跌破月線(20日均線)、且這支股票自己當下也跌破
    月線時，暫停新的BUY(SELL不受影響)。"""

    name = "breakout"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        high_lookback_days = params.get("high_lookback_days", 20)
        low_lookback_days = params.get("low_lookback_days", 10)
        volume_avg_period = params.get("volume_avg_period", 20)
        volume_multiplier = params.get("volume_multiplier", 1.5)
        atr_period = params.get("atr_period", 14)
        atr_multiplier = params.get("atr_multiplier", 2)
        stop_mode = params.get("stop_mode", "atr")  # "atr"(現行:進場價-2倍ATR，固定不動) 或
        # "pct"(移動停損)
        stop_pct = params.get("stop_pct", 0.15)
        require_weekly_trend = params.get("require_weekly_trend", False)  # 現行:True，額外
        # 要求週線級別的趨勢確認，過濾日線假突破。
        weekly_trend_mode = params.get("weekly_trend_mode", "slope")  # "slope"(現行)或
        # "above_ma"，見indicators.weekly_trend_confirmed。
        weekly_ma_period = params.get("weekly_ma_period", 20)
        entry_trigger = params.get("entry_trigger", "edge")  # "edge"(現行) 或 "level"(條件
        # 當天成立就觸發，不要求邊緣)——已用scripts/backtest_breakout_entry_trigger.py
        # 驗證過是no-op(逐檔筆數/報酬完全一致)，不需要改成level，保留參數供其他情境測試用。

        close = bars["close"]
        donchian_upper = bars["high"].rolling(window=high_lookback_days).max().shift(1)
        donchian_lower = bars["low"].rolling(window=low_lookback_days).min().shift(1)
        avg_volume = rolling_avg_volume(bars["volume"], volume_avg_period)
        atr_value = atr(bars["high"], bars["low"], close, atr_period)

        entry_condition = (close > donchian_upper) & (bars["volume"] > volume_multiplier * avg_volume)
        if require_weekly_trend:
            entry_condition = entry_condition & weekly_trend_confirmed(
                close, weekly_ma_period, require_slope_up=(weekly_trend_mode == "slope")
            )
        if entry_trigger == "level":
            entry_edge = entry_condition
        else:
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
