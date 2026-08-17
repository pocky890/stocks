import pandas as pd

from stocks.indicators import rolling_avg_volume, rsi, sma
from stocks.models import Direction, SignalEvent


class GoldenCrossStrategy:
    """均線黃金交叉打分制策略。

    進場條件：
    1. 打分制達5分(score_threshold)：MA5>MA20(+2)、站上MA20(+1)、法人(外資+投信)近5日
       合計買超(+2)、突破20日新高(+2)、當天量>20日均量(+1)、RSI(14)<70未超買(+1)
    2. 60日均線>120日均線
    3. 收盤>240日均線
    4. 月營收年增率≥0%或無資料

    出場條件：
    1. 跌破15%移動停損(stop_pct)，全數出清

    支援模式(回測用)：
    - stop_mode="ma_scaleout"：兩階段出場(跌破5日均線且放量先賣一半，再跌破10日均線
      或5日均線死亡交叉賣剩餘一半)

    斷路器：ON — 全市場同產業≥60%跌破月線時暫停BUY(純看產業寬度)"""

    name = "golden_cross"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        fast = params.get("fast", 5)
        mid = params.get("mid", 10)
        slow = params.get("slow", 20)
        chip_lookback_days = params.get("chip_lookback_days", 5)
        high_lookback_days = params.get("high_lookback_days", 20)
        volume_avg_period = params.get("volume_avg_period", 20)
        score_ma_cross = params.get("score_ma_cross", 2)
        score_above_slow = params.get("score_above_slow", 1)
        score_chip = params.get("score_chip", 2)
        score_breakout = params.get("score_breakout", 2)
        score_volume = params.get("score_volume", 1)
        score_rsi = params.get("score_rsi", 1)
        rsi_period = params.get("rsi_period", 14)
        rsi_overbought = params.get("rsi_overbought", 70)
        score_threshold = params.get("score_threshold", 5)
        stop_mode = params.get("stop_mode", "pct")  # "pct"(現行:單一15%移動停損全出，一買
        # 配一賣) 或 "ma_scaleout"(均線分批出場，一買配兩賣)
        stop_pct = params.get("stop_pct", 0.15)
        require_long_regime = params.get("require_long_regime", False)  # 研究參數(2026-08-16
        # 使用者提議)：額外要求regime_fast_period日均線>regime_slow_period日均線(跟
        # long_swing同一套長期regime判斷)才能進場——這支策略的打分制6項條件全部是5~20天
        # 級別的短線型態，空頭市場的反彈一樣容易達標，缺一道長期趨勢確認。預設False，
        # 未啟用，見scripts/backtest_long_regime_filter.py。
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
        ma_fast = sma(close, fast)
        ma_mid = sma(close, mid)
        ma_slow = sma(close, slow)
        avg_vol = rolling_avg_volume(bars["volume"], volume_avg_period)
        volume_confirm = bars["volume"] > avg_vol

        ma_cross_up = ma_fast > ma_slow
        above_slow = close > ma_slow
        donchian_high = bars["high"].rolling(window=high_lookback_days).max().shift(1)
        breakout_high = close > donchian_high
        not_overbought = rsi(close, rsi_period) < rsi_overbought

        if "foreign_net" in bars.columns and "trust_net" in bars.columns:
            net_flow = (bars["foreign_net"].fillna(0) + bars["trust_net"].fillna(0)).rolling(window=chip_lookback_days).sum()
            chip_backed = net_flow > 0
        else:
            chip_backed = pd.Series(False, index=bars.index)

        score = (
            ma_cross_up.astype(int) * score_ma_cross
            + above_slow.astype(int) * score_above_slow
            + chip_backed.astype(int) * score_chip
            + breakout_high.astype(int) * score_breakout
            + volume_confirm.astype(int) * score_volume
            + not_overbought.astype(int) * score_rsi
        )
        entry_state = score >= score_threshold
        if require_long_regime:
            regime_active = sma(close, regime_fast_period) > sma(close, regime_slow_period)
            entry_state = entry_state & regime_active
        if require_above_long_ma:
            entry_state = entry_state & (close > sma(close, long_ma_period))
        if require_revenue_growth:
            revenue_growth = bars.get("revenue_yoy_growth", pd.Series(float("nan"), index=bars.index))
            growth_ok = (revenue_growth >= revenue_growth_min_pct) | revenue_growth.isna()
            entry_state = entry_state & growth_ok

        # 2026-08-17效能優化：series[t]是label-based lookup(DatetimeIndex.get_loc)，逐bar
        # 呼叫好幾次、乘上上千個bar，profiling量到佔了evaluate()總時間近8成——先轉成numpy
        # array用位置索引，邏輯完全不變，只是indexing方式改變。
        index = bars.index
        close_arr = close.to_numpy()
        entry_state_arr = entry_state.to_numpy()
        score_arr = score.to_numpy()
        ma_cross_up_arr = ma_cross_up.to_numpy()
        above_slow_arr = above_slow.to_numpy()
        chip_backed_arr = chip_backed.to_numpy()
        breakout_high_arr = breakout_high.to_numpy()
        volume_confirm_arr = volume_confirm.to_numpy()
        not_overbought_arr = not_overbought.to_numpy()

        def entry_hits(i: int) -> list[str]:
            return [
                label
                for label, arr in [
                    (f"MA{fast}>MA{slow}", ma_cross_up_arr),
                    (f"站上MA{slow}", above_slow_arr),
                    (f"法人近{chip_lookback_days}日買超", chip_backed_arr),
                    (f"突破{high_lookback_days}日新高", breakout_high_arr),
                    ("量增", volume_confirm_arr),
                    (f"RSI未超買(<{rsi_overbought})", not_overbought_arr),
                ]
                if arr[i]
            ]

        if stop_mode == "pct":
            events: list[SignalEvent] = []
            in_position = False
            stop = None

            for i, t in enumerate(index):
                c = close_arr[i]
                if in_position:
                    if c < stop:
                        events.append(
                            SignalEvent(symbol, self.name, Direction.SELL, c, t, f"跌破{stop_pct * 100:.0f}%移動停損 {stop:.2f}")
                        )
                        in_position = False
                        stop = None
                    else:
                        stop = max(stop, c * (1 - stop_pct))
                elif entry_state_arr[i]:
                    stop = c * (1 - stop_pct)
                    hits = entry_hits(i)
                    events.append(
                        SignalEvent(
                            symbol,
                            self.name,
                            Direction.BUY,
                            c,
                            t,
                            f"打分{score_arr[i]}分達標({'、'.join(hits)})，{stop_pct * 100:.0f}%移動停損 {stop:.2f}",
                        )
                    )
                    in_position = True

            return events

        below_fast_confirmed = (close < ma_fast) & volume_confirm
        prev_below_fast_confirmed = below_fast_confirmed.shift(1).fillna(False).astype(bool)
        break_half_edge = below_fast_confirmed & ~prev_below_fast_confirmed

        exit_remaining_condition = (close < ma_mid) | (ma_fast < ma_slow)
        prev_exit_remaining = exit_remaining_condition.shift(1).fillna(False).astype(bool)
        break_remaining_edge = exit_remaining_condition & ~prev_exit_remaining

        ma_mid_arr = ma_mid.to_numpy()
        ma_fast_arr = ma_fast.to_numpy()
        ma_slow_arr = ma_slow.to_numpy()
        break_half_edge_arr = break_half_edge.to_numpy()
        break_remaining_edge_arr = break_remaining_edge.to_numpy()

        events: list[SignalEvent] = []
        position = 0  # 0=空手, 2=全倉, 1=剩一半

        for i, t in enumerate(index):
            c = close_arr[i]
            # 2026-08-17使用者發現：原本這裡是三個獨立的if，如果「今天才把剩餘半倉出清
            # 完畢」同一天又剛好重新打分達標，會在同一天既賣又買，訊息看起來自相矛盾——這是
            # 寫法疏漏，不是刻意設計。position_before記住這根bar開盤前的部位狀態，只有「今天
            # 開盤前就已經空手」才允許進場——今天才出清完的部位要等明天才能重新進場，跟其他
            # 策略用elif達到的效果一致：出場後最快隔天分數還達標就能重新進場，不會卡死。
            position_before = position
            if position == 2 and break_half_edge_arr[i]:
                events.append(
                    SignalEvent(symbol, self.name, Direction.SELL, c, t, f"跌破{fast}日均線且量能放大，賣出一半")
                )
                position = 1
            if position == 1 and break_remaining_edge_arr[i]:
                reasons = []
                if close_arr[i] < ma_mid_arr[i]:
                    reasons.append(f"跌破{mid}日均線")
                if ma_fast_arr[i] < ma_slow_arr[i]:
                    reasons.append(f"{fast}日均線跌破{slow}日均線")
                events.append(
                    SignalEvent(symbol, self.name, Direction.SELL, c, t, "、".join(reasons) + "，賣出剩餘一半")
                )
                position = 0
            if position_before == 0 and entry_state_arr[i]:
                hits = entry_hits(i)
                events.append(
                    SignalEvent(symbol, self.name, Direction.BUY, c, t, f"打分{score_arr[i]}分達標({'、'.join(hits)})")
                )
                position = 2

        return events
