import pandas as pd

from stocks.indicators import atr, rolling_avg_volume, rsi, sma
from stocks.models import Direction, SignalEvent


class TrustMomentumStrategy:
    """投信買超動能策略，跟chip_momentum同一套邏輯，主訊號換成投信(trust_net)買超。

    進場：近15日(cum_window_days)累積買超為正 + 近3日內至少2日買超(entry_mode="window10_3")
    + RSI(14)未超買(<70) + 60日均線>120日均線(require_long_regime，現行:True，
    2026-08-16加的長期regime濾網) + 月營收年增率>=0%(require_revenue_growth，現行:
    True，2026-08-16加的基本面濾網，見db.attach_monthly_revenue_growth())。240日年線
    濾網(require_above_long_ma)測過對這支策略沒有明顯增量(疊加regime後獲利因子打平)，
    未採用。

    出場：現行(config.json):stop_mode="volume_alert_scaleout"——高檔跌破alert_ma_period
    (現行:10)日均線且成交量>alert_volume_multiplier(現行:1.5)倍均量時，先賣出一半
    ("爆量出貨警示")，剩餘半倉改用stop_pct(現行:0.20，2026-08-16從0.15拉寬)移動停損
    出場；一買配兩賣，要用simulate_scaleout_trades配對。拉寬理由：使用者提議進場濾網
    已經加嚴、出場能否放寬換報酬，實測(scripts/backtest_wider_exit_stops.py)剩餘半倉
    停損15%→20%是溫和的贏(10年獲利因子5.63→6.73、YTD 1.50→2.09都變好)，但25%就過頭
    (YTD轉虧、樣本剩7筆)，故只採用20%這一檔；同批也測過拉高alert_volume_multiplier讓
    警示更晚觸發，10年/YTD一致變差，未採用。也支援單一移動停損(stop_mode="pct")、ATR
    移動停損("atr")、分批停損("tiered_pct")。

    進場是level-triggered(條件當天成立就觸發，不要求剛從False轉True)，停損出場後最快
    隔天條件仍成立就能重新進場(除非設定cooldown_days>0，研究參數，見下方)。2026-08-17
    修正：出清完畢的當天不會立刻重新進場，要等到隔天才能重新觸發BUY——避免同一天同一
    支股票同時出現BUY和SELL訊號，讓通知看起來自相矛盾。

    斷路器：適用——全市場同產業≥60%股票跌破月線(20日均線)時暫停新的BUY(SELL不受影響)。
    2026-08-16拿掉了「自己當下也跌破月線」這道AND條件(改成純看產業寬度，config.
    circuit_breaker_own_ma_period=None)，理由見circuit_breaker.py開頭說明。

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
        require_above_long_ma = params.get("require_above_long_ma", False)  # 研究參數(2026-08-16
        # 使用者轉述Gemini建議)：額外要求收盤價>long_ma_period(現行:240，約年線)日均線，
        # 跟require_long_regime(60/120日均線交叉)不同，這個量的是長期絕對位階。預設
        # False，未啟用，見scripts/backtest_macro_regime_filters.py。
        long_ma_period = params.get("long_ma_period", 240)
        require_revenue_growth = params.get("require_revenue_growth", False)  # 研究參數
        # (2026-08-16)：額外要求月營收年增率(revenue_yoy_growth，由db.attach_monthly_
        # revenue_growth()接到bars上，已處理FinMind公告日+10天緩衝的look-ahead)>=
        # revenue_growth_min_pct(現行:0.0)。缺資料時當NaN，一律當作「未知不擋」。全
        # 觀察清單10年回測對trust_momentum效果明確(獲利因子5.37→5.49，20支已知下跌
        # 很兇的股票上2.22→3.12)，見scripts/backtest_revenue_growth_filter.py。預設
        # False，未啟用。
        revenue_growth_min_pct = params.get("revenue_growth_min_pct", 0.0)

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
        if require_long_regime:
            regime_active = sma(close, regime_fast_period) > sma(close, regime_slow_period)
            entry_condition = entry_condition & regime_active
        if require_above_long_ma:
            entry_condition = entry_condition & (close > sma(close, long_ma_period))
        if require_entry_volume:
            avg_vol_entry = rolling_avg_volume(bars["volume"], entry_volume_avg_period)
            entry_condition = entry_condition & (bars["volume"] > entry_volume_multiplier * avg_vol_entry)
        if require_revenue_growth:
            revenue_growth = bars.get("revenue_yoy_growth", pd.Series(float("nan"), index=bars.index))
            growth_ok = (revenue_growth >= revenue_growth_min_pct) | revenue_growth.isna()
            entry_condition = entry_condition & growth_ok

        # 2026-08-17效能優化：series[t]是label-based lookup(DatetimeIndex.get_loc)，逐bar
        # 呼叫好幾次、乘上上千個bar，profiling量到佔了evaluate()總時間近8成——先轉成numpy
        # array用位置索引，邏輯完全不變，只是indexing方式改變。
        index = bars.index
        close_arr = close.to_numpy()
        entry_condition_arr = entry_condition.to_numpy()

        if stop_mode == "volume_alert_scaleout":
            ma_alert = sma(close, alert_ma_period)
            avg_vol_alert = rolling_avg_volume(bars["volume"], alert_volume_avg_period)
            volume_alert_condition = (close < ma_alert) & (bars["volume"] > alert_volume_multiplier * avg_vol_alert)
            volume_alert_condition_arr = volume_alert_condition.to_numpy()

            events: list[SignalEvent] = []
            position = 0  # 0=空手, 2=全倉, 1=剩半倉
            stop = None
            cooldown_remaining = 0

            for i, t in enumerate(index):
                c = close_arr[i]
                # 2026-08-17使用者發現：原本這裡是三個獨立的if，如果「今天才把剩餘半倉出清
                # 完畢」同一天又剛好重新達到進場門檻，會在同一天既賣又買，訊息看起來自相
                # 矛盾——這是寫法疏漏，不是刻意設計。position_before記住這根bar開盤前的部位
                # 狀態，只有「今天開盤前就已經空手」才允許進場(或開始計算冷卻期)——今天才
                # 出清完的部位要等明天才能重新進場，跟其他策略(atr模式/long_swing等)用elif
                # 達到的效果一致：停損出場後最快隔天條件還成立就能重新進場，不會卡死。
                position_before = position
                if position == 2 and volume_alert_condition_arr[i]:
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
                        cooldown_remaining = cooldown_days
                if position_before == 0 and cooldown_remaining > 0:
                    cooldown_remaining -= 1
                elif position_before == 0 and entry_condition_arr[i]:
                    events.append(
                        SignalEvent(
                            symbol,
                            self.name,
                            Direction.BUY,
                            c,
                            t,
                            f"投信近{chip_window_days}日{chip_min_buy_days}天以上買超(未超買)",
                        )
                    )
                    position = 2

            return events

        if stop_mode == "tiered_pct":
            events: list[SignalEvent] = []
            in_position = False
            half_sold = False
            stop_half = None
            stop_full = None
            cooldown_remaining = 0

            for i, t in enumerate(index):
                c = close_arr[i]
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
                elif entry_condition_arr[i]:
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
        atr_arr = atr_value.to_numpy()

        def next_stop(c: float, atr_val: float) -> float:
            if stop_mode == "pct":
                return c * (1 - stop_pct)
            return c - atr_multiplier * atr_val

        stop_label = f"{stop_pct * 100:.0f}%移動停損" if stop_mode == "pct" else "ATR移動停損"

        events: list[SignalEvent] = []
        in_position = False
        stop = None
        cooldown_remaining = 0

        for i, t in enumerate(index):
            c = close_arr[i]
            if in_position:
                if c < stop:
                    events.append(SignalEvent(symbol, self.name, Direction.SELL, c, t, f"跌破{stop_label} {stop:.2f}"))
                    in_position = False
                    stop = None
                    cooldown_remaining = cooldown_days
                elif stop_mode == "pct" or not pd.isna(atr_arr[i]):
                    stop = max(stop, next_stop(c, atr_arr[i]))
            elif cooldown_remaining > 0:
                cooldown_remaining -= 1
            elif entry_condition_arr[i] and (stop_mode == "pct" or not pd.isna(atr_arr[i])):
                stop = next_stop(c, atr_arr[i])
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
