import pandas as pd

from stocks.indicators import atr, rolling_avg_volume, sma, weekly_trend_confirmed
from stocks.models import Direction, SignalEvent


class BreakoutStrategy:
    """突破策略。

    進場條件：
    1. 收盤創前20日新高(唐奇安通道上軌，邊緣觸發)
    2. 成交量>1.5倍20日均量
    3. 週線MA20斜率向上
    4. 60日均線>120日均線
    5. 收盤>240日均線
    6. 月營收年增率≥0%或無資料

    出場條件：
    1. 收盤跌破前10日最低，或
    2. 跌破「進場價-3倍14日ATR」固定停損(不移動)
    兩者先到者為準。

    支援模式(回測用)：
    - stop_mode="pct"：移動停損
    - entry_trigger="level"：條件當天成立即觸發(非邊緣，已驗證等效)

    斷路器：ON — 全市場同產業≥60%跌破月線時暫停BUY(純看產業寬度)"""

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
        require_long_regime = params.get("require_long_regime", False)  # 研究參數(2026-08-16
        # 使用者提議)：額外要求regime_fast_period日均線>regime_slow_period日均線(跟
        # long_swing同一套長期regime判斷)才能進場，過濾空頭市場裡的反彈假突破新高。
        # 預設False，未啟用，見scripts/backtest_long_regime_filter.py。
        regime_fast_period = params.get("regime_fast_period", 60)
        regime_slow_period = params.get("regime_slow_period", 120)
        require_above_long_ma = params.get("require_above_long_ma", False)  # 研究參數(2026-08-16
        # 使用者轉述Gemini建議)：額外要求收盤價>long_ma_period(現行:240，約年線)日均線，
        # 跟require_long_regime(60/120日均線交叉)不同，這個量的是長期絕對位階。預設
        # False，未啟用，見scripts/backtest_macro_regime_filters.py。
        long_ma_period = params.get("long_ma_period", 240)
        require_revenue_growth = params.get("require_revenue_growth", False)  # 研究參數
        # (2026-08-16)：額外要求月營收年增率(revenue_yoy_growth，由db.attach_monthly_
        # revenue_growth()接到bars上，已處理FinMind公告日+10天緩衝的look-ahead)>=
        # revenue_growth_min_pct(現行:0.0)。缺資料時當NaN，一律當作「未知不擋」。
        # 見scripts/backtest_revenue_growth_filter.py。預設False，未啟用。
        revenue_growth_min_pct = params.get("revenue_growth_min_pct", 0.0)

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
        if require_long_regime:
            regime_active = sma(close, regime_fast_period) > sma(close, regime_slow_period)
            entry_condition = entry_condition & regime_active
        if require_above_long_ma:
            entry_condition = entry_condition & (close > sma(close, long_ma_period))
        if require_revenue_growth:
            revenue_growth = bars.get("revenue_yoy_growth", pd.Series(float("nan"), index=bars.index))
            growth_ok = (revenue_growth >= revenue_growth_min_pct) | revenue_growth.isna()
            entry_condition = entry_condition & growth_ok
        if entry_trigger == "level":
            entry_edge = entry_condition
        else:
            prev_entry = entry_condition.shift(1).fillna(False).astype(bool)
            entry_edge = entry_condition & ~prev_entry

        def next_stop(c: float, atr_val: float) -> float:
            if stop_mode == "pct":
                return c * (1 - stop_pct)
            return c - atr_multiplier * atr_val

        stop_label = f"{stop_pct * 100:.0f}%移動停損" if stop_mode == "pct" else "停損"

        # 2026-08-17效能優化：series[t]是label-based lookup(DatetimeIndex.get_loc)，逐bar
        # 呼叫好幾次、乘上上千個bar，profiling量到佔了evaluate()總時間近8成——先轉成numpy
        # array用位置索引，邏輯完全不變，只是indexing方式改變。
        index = bars.index
        close_arr = close.to_numpy()
        donchian_upper_arr = donchian_upper.to_numpy()
        donchian_lower_arr = donchian_lower.to_numpy()
        atr_arr = atr_value.to_numpy()
        entry_edge_arr = entry_edge.to_numpy()

        events: list[SignalEvent] = []
        in_position = False
        stop = None

        for i, t in enumerate(index):
            c = close_arr[i]
            if in_position:
                reasons = []
                if not pd.isna(donchian_lower_arr[i]) and c < donchian_lower_arr[i]:
                    reasons.append(f"跌破前{low_lookback_days}日最低{donchian_lower_arr[i]:.2f}")
                if c < stop:
                    reasons.append(f"跌破{stop_label}{stop:.2f}")
                if reasons:
                    events.append(SignalEvent(symbol, self.name, Direction.SELL, c, t, "、".join(reasons)))
                    in_position = False
                    stop = None
                elif stop_mode == "pct":
                    stop = max(stop, next_stop(c, atr_arr[i]))
            elif entry_edge_arr[i] and not pd.isna(donchian_upper_arr[i]) and (stop_mode == "pct" or not pd.isna(atr_arr[i])):
                stop = next_stop(c, atr_arr[i])
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
