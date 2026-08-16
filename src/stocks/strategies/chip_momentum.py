import pandas as pd

from stocks.indicators import atr, rolling_avg_volume, rsi, sma
from stocks.models import Direction, SignalEvent


class ChipMomentumStrategy:
    """外資買超動能策略。

    進場：近5天(ratio_window_days)外資淨買超加總為正，且加總/近同窗口總成交量加總>10%
    (ratio_threshold，entry_mode="ratio")+ RSI(14)未超買(<70) + 60日均線>120日均線
    (require_long_regime，現行:True，2026-08-16加的長期regime濾網) + 月營收年增率
    >=0%(require_revenue_growth，現行:True，2026-08-16加的基本面濾網，見
    db.attach_monthly_revenue_growth())。也支援entry_mode="streak"(連續剛好
    chip_streak_days天買超，原本的預設)、"window"(累積為正+近期頻率門檻)。

    出場：現行(config.json):stop_mode="volume_alert_scaleout"——高檔跌破alert_ma_period
    (現行:10)日均線且成交量>alert_volume_multiplier(現行:1.5)倍均量時，先賣出一半
    ("爆量出貨警示")，剩餘半倉改用stop_pct(15%)移動停損出場；一買配兩賣，要用
    simulate_scaleout_trades配對。也支援單一15%移動停損(stop_mode="pct")、2.5倍ATR
    移動停損("atr")、分批停損("tiered_pct")。

    斷路器：適用——全市場同產業≥60%股票跌破月線(20日均線)時暫停新的BUY(SELL不受影響)。
    2026-08-16拿掉了「自己當下也跌破月線」這道AND條件(改成純看產業寬度，config.
    circuit_breaker_own_ma_period=None)，理由見circuit_breaker.py開頭說明。

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
        entry_mode = params.get("entry_mode", "streak")  # "ratio"(現行:近
        # ratio_window_days日淨買超加總為正，且淨買超加總/總成交量加總>ratio_threshold，
        # 用集中度取代天數頻率)、"streak"(不設entry_mode時的預設寫法，原本現行:連續剛好
        # chip_streak_days天買超) 或 "window"(近cum_window_days日累積買超為正，且近
        # recent_window_days日內至少recent_min_buy_days天買超，比連續買超寬鬆)
        cum_window_days = params.get("cum_window_days", 10)
        recent_window_days = params.get("recent_window_days", 3)
        recent_min_buy_days = params.get("recent_min_buy_days", 2)
        ratio_window_days = params.get("ratio_window_days", 5)
        ratio_threshold = params.get("ratio_threshold", 0.08)
        require_entry_volume = params.get("require_entry_volume", False)  # 研究參數(2026-08-16
        # 使用者轉述Gemini建議)：進場當天額外要求成交量>entry_volume_multiplier倍均量("帶量
        # 點火")，預設False，未啟用，見scripts/backtest_chip_trust_momentum_volume_filter.py。
        entry_volume_avg_period = params.get("entry_volume_avg_period", 20)
        entry_volume_multiplier = params.get("entry_volume_multiplier", 1.5)
        alert_ma_period = params.get("alert_ma_period", 5)  # stop_mode="volume_alert_scaleout"
        # 專用：高檔跌破這條均線且爆量時，觸發賣出一半("爆量出貨警示")，見下方。
        alert_volume_avg_period = params.get("alert_volume_avg_period", 20)
        alert_volume_multiplier = params.get("alert_volume_multiplier", 1.5)
        require_long_regime = params.get("require_long_regime", False)  # 研究參數(2026-08-16
        # 使用者提議)：額外要求regime_fast_period日均線>regime_slow_period日均線(跟
        # long_swing同一套長期regime判斷)才能進場，過濾「股票已經進入長期空頭、法人買超
        # 只是空頭市場裡的反彈雜訊」的情況。預設False，未啟用，見
        # scripts/backtest_long_regime_filter.py。
        regime_fast_period = params.get("regime_fast_period", 60)
        regime_slow_period = params.get("regime_slow_period", 120)
        require_within_drawdown_limit = params.get("require_within_drawdown_limit", False)  # 研究
        # 參數(2026-08-16使用者轉述Gemini建議)：額外要求收盤價沒有從過去drawdown_lookback_
        # days(現行:252，約一年)的高點回落超過max_drawdown_from_high_pct(現行:40%)，
        # 用絕對跌幅(而非均線交叉)判斷「基本面/籌碼可能已經嚴重破壞」，反應可能比均線
        # regime濾網快(均線要花時間才會死亡交叉，但股價相對高點的位置是即時的)。預設
        # False，未啟用，見scripts/backtest_macro_regime_filters.py。
        drawdown_lookback_days = params.get("drawdown_lookback_days", 252)
        max_drawdown_from_high_pct = params.get("max_drawdown_from_high_pct", 0.40)
        require_above_long_ma = params.get("require_above_long_ma", False)  # 研究參數(2026-08-16
        # 使用者轉述Gemini建議)：額外要求收盤價>long_ma_period(現行:240，約年線)日均線——
        # 跟require_long_regime(60/120日均線交叉，量的是短中期趨勢方向)不同，這個量的是
        # 長期絕對位階，理論上更難在空頭市場的反彈裡被騙過(反彈要噴到站回年線比騙過
        # 60/120交叉難很多)。預設False，未啟用，見scripts/backtest_macro_regime_filters.py。
        long_ma_period = params.get("long_ma_period", 240)
        require_revenue_growth = params.get("require_revenue_growth", False)  # 研究參數
        # (2026-08-16)：額外要求月營收年增率(revenue_yoy_growth，由db.attach_monthly_
        # revenue_growth()接到bars上，已處理FinMind公告日+10天緩衝的look-ahead)>=
        # revenue_growth_min_pct(現行:0.0，即營收年增率不能轉負)。缺資料(還沒回補、或
        # 這支股票根本沒有月營收如ETF)時當NaN，一律當作「未知不擋」，不主動排除。全
        # 觀察清單10年回測：加總報酬犧牲9%~24%換獲利因子普遍+8%~22%，見scripts/
        # backtest_revenue_growth_filter.py。預設False，未啟用。
        revenue_growth_min_pct = params.get("revenue_growth_min_pct", 0.0)

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
        if require_long_regime:
            regime_active = sma(close, regime_fast_period) > sma(close, regime_slow_period)
            entry_condition = entry_condition & regime_active
        if require_within_drawdown_limit:
            rolling_high = bars["high"].rolling(drawdown_lookback_days).max().shift(1)
            within_drawdown_limit = close > (1 - max_drawdown_from_high_pct) * rolling_high
            entry_condition = entry_condition & within_drawdown_limit
        if require_above_long_ma:
            entry_condition = entry_condition & (close > sma(close, long_ma_period))
        if require_entry_volume:
            avg_vol_entry = rolling_avg_volume(bars["volume"], entry_volume_avg_period)
            entry_condition = entry_condition & (bars["volume"] > entry_volume_multiplier * avg_vol_entry)
        if require_revenue_growth:
            revenue_growth = bars.get("revenue_yoy_growth", pd.Series(float("nan"), index=bars.index))
            growth_ok = (revenue_growth >= revenue_growth_min_pct) | revenue_growth.isna()
            entry_condition = entry_condition & growth_ok
        prev_entry = entry_condition.shift(1).fillna(False).astype(bool)
        entry_edge = entry_condition & ~prev_entry

        if stop_mode == "volume_alert_scaleout":
            ma_alert = sma(close, alert_ma_period)
            avg_vol_alert = rolling_avg_volume(bars["volume"], alert_volume_avg_period)
            volume_alert_condition = (close < ma_alert) & (bars["volume"] > alert_volume_multiplier * avg_vol_alert)

            events: list[SignalEvent] = []
            position = 0  # 0=空手, 2=全倉, 1=剩半倉
            stop = None

            for t in bars.index:
                c = close[t]
                if position == 2 and volume_alert_condition[t]:
                    events.append(
                        SignalEvent(
                            symbol,
                            self.name,
                            Direction.SELL,
                            c,
                            t,
                            f"跌破{alert_ma_period}日均線且量能>{alert_volume_multiplier}倍均量(爆量出貨警示)，賣出一半",
                        )
                    )
                    position = 1
                    stop = c * (1 - stop_pct)
                if position == 1:
                    stop = max(stop, c * (1 - stop_pct))
                    if c < stop:
                        events.append(
                            SignalEvent(symbol, self.name, Direction.SELL, c, t, f"跌破{stop_pct * 100:.0f}%移動停損 {stop:.2f}，賣出剩餘一半")
                        )
                        position = 0
                        stop = None
                if position == 0 and entry_edge[t]:
                    events.append(
                        SignalEvent(symbol, self.name, Direction.BUY, c, t, f"外資連{chip_streak_days}日買超(未超買)")
                    )
                    position = 2

            return events

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
