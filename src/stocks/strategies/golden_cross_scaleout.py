import pandas as pd

from stocks.indicators import rolling_avg_volume, rsi, sma
from stocks.models import Direction, SignalEvent


class GoldenCrossScaleOutStrategy:
    """均線黃金交叉打分制策略。

    進場：以下6項各自加分，總分達到5分(score_threshold)才進場，level-triggered(當天
    分數達標就進場，不要求前一天未達標)：
      MA5 > MA20                 +2
      收盤站上MA20                +1
      三大法人(外資+投信)近5日合計買超    +2
      收盤突破20日新高(不含當天)        +2
      當天成交量 > 20日均量            +1
      RSI(14)未超買(<70)             +1

    達標分數之外，另外要求60日均線>120日均線(require_long_regime，現行:True) + 收盤價
    >240日均線/年線(require_above_long_ma，現行:True，跟regime疊加、不是取代) + 月營收
    年增率>=0%(require_revenue_growth，現行:True，2026-08-16加的基本面濾網，見
    db.attach_monthly_revenue_growth())才能進場。這三道濾網都是2026-08-16加的，
    全觀察清單10年獲利因子2.54→2.81(疊加regime+MA240+營收後)、20支已知近年下跌很兇
    的股票上獲利因子0.79→0.92~0.96，細節見scripts/backtest_long_regime_filter.py/
    backtest_macro_regime_filters.py/backtest_revenue_growth_filter.py。

    出場：跌破15%移動停損(stop_mode="pct")，一買配一賣。

    也支援stop_mode="ma_scaleout"：分兩階段出場，不是一次全出：
      階段1(賣一半)：收盤跌破5日均線，且當天成交量>20日均量
      階段2(賣剩餘一半)：收盤跌破10日均線，或5日均線跌破20日均線(死亡交叉)
    這個模式一買配兩賣，要用simulate_scaleout_trades配對，不能套simulate_round_trips。

    斷路器：適用——全市場同產業≥60%股票跌破月線(20日均線)時暫停新的BUY(SELL不受影響)。
    2026-08-16拿掉了「自己當下也跌破月線」這道AND條件(改成純看產業寬度，config.
    circuit_breaker_own_ma_period=None)，理由見circuit_breaker.py開頭說明。"""

    name = "golden_cross_scaleout"

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
            if position == 0 and entry_state_arr[i]:
                hits = entry_hits(i)
                events.append(
                    SignalEvent(symbol, self.name, Direction.BUY, c, t, f"打分{score_arr[i]}分達標({'、'.join(hits)})")
                )
                position = 2

        return events
