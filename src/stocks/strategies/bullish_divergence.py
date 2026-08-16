import pandas as pd

from stocks.indicators import atr, macd, rolling_avg_volume, rsi, sma, stochastic_kd
from stocks.models import Direction, SignalEvent


class BullishDivergenceStrategy:
    """背離抄底策略。

    初步訊號：收盤價創20日新低，但RSI(14)未跟著創新低(背離，代表下跌動能減弱)，
    且RSI<35(還在偏弱區間，避免已強勢反彈完才進場)

    確認進場(require_reversal_confirm)：初步訊號出現後不會立刻進場，最多等待10個交易日
    (reversal_confirm_max_wait_days)，直到以下確認訊號中至少1項成立
    (reversal_confirm_min_signals)才進場：
      - 收盤站上5日均線，或站上前一日高點
      - MACD柱狀圖比前一天回升(require_reversal_macd)
    等待期間若出現更低的新低+背離，用新訊號重新起算等待。

    出場(現行:stop_mode="structural"+enable_tiered_profit，一買配兩賣，跟golden_cross_
    scaleout的ma_scaleout模式同樣要用simulate_scaleout_trades配對，不能套simulate_
    round_trips)，三階段：
      ①初始結構停損防接刀：進場K棒最低點再往下5%緩衝(structural_stop_buffer_pct)
      ②獲利達12%(tiered_target_pct)或觸及60日均線(tiered_ma_period)先賣一半，剩餘部位
        停損上移至成本價保本(move_stop_to_breakeven_after_tier)
      ③剩餘部位改用15%(stop_pct)寬幅移動停損，讓真正的大反轉抱好抱滿

    也支援單一停損：固定15%移動停損("pct")、ATR停損("atr")、分批停損("tiered_pct")、
    或不搭配tiered_profit的純結構停損("structural"，注意：固定不動又沒有其他出場條件，
    獲利部位會一直持有到觸及停損為止，見structural_trail_after_pct/enable_tiered_profit
    參數註解的實測說明)。

    斷路器：豁免（在CIRCUIT_BREAKER_EXEMPT_STRATEGIES清單內）。斷路器本身的條件是「全市場
    同產業≥60%股票跌破月線、且這支股票自己當下也跌破月線」才擋新BUY——但這支策略的進場
    前提就是「自己正跌破月線」，兩者天生衝突，故完全跳過檢查(見circuit_breaker.py)。"""

    name = "bullish_divergence"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        lookback_days = params.get("lookback_days", 20)
        rsi_period = params.get("rsi_period", 14)
        rsi_ceiling = params.get("rsi_ceiling", 40)  # 現行(config.json):35。用
        # scripts/backtest_bullish_divergence_user_proposal.py驗證過：比原本的30全觀察清單
        # 10年加總報酬+22%(7842.8→9583.2)、勝率/平均報酬幾乎不變，才改成35當預設。
        atr_period = params.get("atr_period", 14)
        atr_multiplier = params.get("atr_multiplier", 2.5)
        stop_mode = params.get("stop_mode", "pct")  # "atr"、"pct"(固定15%移動停損)、
        # "tiered_pct"(分批停損) 或 "structural"(現行:進場K棒低點-緩衝%，固定不動，
        # 搭配enable_tiered_profit=True形成三階段出場，見class docstring)
        stop_pct = params.get("stop_pct", 0.15)
        stop_pct_half = params.get("stop_pct_half", 0.08)
        stop_pct_full = params.get("stop_pct_full", 0.15)
        confirm_next_day = params.get("confirm_next_day", False)  # 額外要求隔天不再破前低
        # +收盤收高才進場(跟capitulation_reversal同一套確認邏輯)。預設False，未啟用。
        require_macd_turn = params.get("require_macd_turn", False)  # 初步訊號額外要求MACD
        # 柱狀圖比前一天回升。預設False，未啟用。
        require_kd_bullish = params.get("require_kd_bullish", False)  # 初步訊號額外要求
        # K>D(偏多)。預設False，未啟用。
        require_capitulation_volume = params.get("require_capitulation_volume", False)  # 研究
        # 參數(2026-08-16使用者轉述Gemini建議)：初步訊號(創新低)當天額外要求成交量放大
        # (恐慌性拋售換手)。預設False，未啟用，見
        # scripts/backtest_bullish_divergence_volume_confirm.py。
        capitulation_volume_avg_period = params.get("capitulation_volume_avg_period", 20)
        capitulation_volume_multiplier = params.get("capitulation_volume_multiplier", 1.5)
        require_long_uptrend_intact = params.get("require_long_uptrend_intact", False)  # 研究
        # 參數(2026-08-16使用者轉述Gemini建議)：額外要求long_trend_ma_period(現行:120)日
        # 均線斜率向上，才准許抄底——不是要求「當下站上」這條均線(那樣會跟抄底的前提
        # 矛盾)，是區分「長線仍是多頭、只是短線跌深」(抄底有效) vs「結構性空頭裡的死貓
        # 反彈」(抄底容易被巴)，跟long_swing原本就有的regime判斷同一個精神，套用在
        # 抄底類策略上。預設False，未啟用，見scripts/backtest_macro_regime_filters.py。
        long_trend_ma_period = params.get("long_trend_ma_period", 120)
        long_trend_slope_lookback = params.get("long_trend_slope_lookback", 20)
        require_reversal_confirm = params.get("require_reversal_confirm", False)  # 現行:True。
        # 初步訊號出現後不直接進場，改成等待價格反轉確認訊號出現才進場(見class docstring)。
        reversal_confirm_ma_period = params.get("reversal_confirm_ma_period", 5)  # 確認訊號
        # 「站上均線」用哪一條均線，預設5日。
        require_reversal_kd = params.get("require_reversal_kd", False)  # 確認訊號額外納入
        # KD(K>D)。預設False，未啟用。
        require_reversal_macd = params.get("require_reversal_macd", False)  # 現行:True。
        # 確認訊號額外納入MACD柱狀圖回升。
        require_reversal_volume = params.get("require_reversal_volume", False)  # 研究參數
        # (2026-08-16使用者轉述Gemini建議)：確認訊號額外納入「反轉當天量能放大」(換手量)，
        # 跟require_reversal_kd/require_reversal_macd同一套confirm_signals機制、一起計入
        # reversal_confirm_min_signals。預設False，未啟用，見
        # scripts/backtest_bullish_divergence_volume_confirm.py。
        reversal_volume_avg_period = params.get("reversal_volume_avg_period", 20)
        reversal_volume_multiplier = params.get("reversal_volume_multiplier", 1.5)
        reversal_confirm_macd_positive = params.get("reversal_confirm_macd_positive", False)  # MACD
        # 確認訊號額外要求柱狀圖轉正(>0)，不是只要求比昨天回升。預設False，未啟用。
        reversal_confirm_macd_streak_days = params.get("reversal_confirm_macd_streak_days", 1)  # MACD
        # 確認訊號要求連續N天都比前一天回升，預設1天(只要當天回升即可)。
        reversal_confirm_min_signals = params.get("reversal_confirm_min_signals", 1)  # 「價格
        # 確認」「KD偏多」「MACD回升」幾項確認訊號(只計入有啟用的)裡至少要有幾項同時成立，
        # 預設1(任一成立即可)。
        reversal_confirm_max_wait_days = params.get("reversal_confirm_max_wait_days", 10)  # 等待
        # 確認訊號最多幾個交易日，超過就放棄這次初步訊號。
        structural_stop_buffer_pct = params.get("structural_stop_buffer_pct", 0.02)  # stop_mode=
        # "structural"時，停損=進場K棒最低點再往下的緩衝百分比。現行(config.json):0.05——
        # 2%緩衝太貼近進場K棒本身、容易被正常回測雜訊洗出場，放寬到5%後(搭配下面
        # enable_tiered_profit)全觀察清單10年勝率54.3%→56.5%、加總報酬3744.7→3975.1，
        # 兩項都小幅變好，才改成5%當預設。
        structural_trail_after_pct = params.get("structural_trail_after_pct", None)  # stop_mode=
        # "structural"時，獲利達到這個百分比後改成用stop_pct移動停損取代固定的結構停損——
        # 現行None代表不使用這個簡化版switch(改用下面enable_tiered_profit的完整三階段
        # 架構)。注意：如果structural且這裡是None、enable_tiered_profit也是False，停損
        # 進場後固定不動又沒有其他出場條件，獲利部位會一直持有到停損被觸及為止(可能持有
        # 數年)——一開始就是因為這樣，backtest才會測出「全觀察清單10年只有115筆完整
        # 進出場、且勝率0%」的假象：26/28檔股票的部位其實還「持有中」從未真正出場(部分
        # 未實現獲利超過1000%)，虧損的才會被停損出場變成可統計的完整交易，勝率0%只是
        # 統計口徑造成的假象，不是真的沒有一筆賺錢。
        enable_tiered_profit = params.get("enable_tiered_profit", False)  # stop_mode=
        # "structural"時額外啟用的三階段出場架構(一買配兩賣，見class docstring)。現行
        # (config.json):True。用scripts/backtest_bullish_divergence_user_proposal.py
        # 驗證過(simulate_scaleout_trades配對)：勝率是測過的版本裡最高(45.3%→56.9%)、
        # 最大回撤也最小(-478.5→-202.8)，risk management本身有效；代價是總報酬
        # (7842.8→3019.9)、獲利因子(3.82→3.06)都明顯變差——半倉在12%獲利或觸及季線就先
        # 落袋，會犧牲掉少數幾筆抱到滿的巨大反轉。使用者2026-08-17確認要的是「更高勝率+
        # 更平穩」勝過總報酬最大化，故採用為預設——這個取捨判斷跟這個codebase其他策略
        # (long_swing/trend_following/atr_breakout)平常「獲利因子+總報酬優先」的預設不同，
        # 是使用者明確的例外選擇，不是判斷標準本身改變。
        tiered_target_pct = params.get("tiered_target_pct", 0.12)  # 賣出一半的獲利門檻。
        tiered_ma_period = params.get("tiered_ma_period", 60)  # 賣出一半的另一個觸發條件：
        # 收盤觸及/站上這條均線(季線，反彈碰壓力最容易回檔)，跟tiered_target_pct任一
        # 成立即可，不用兩者都到。
        move_stop_to_breakeven_after_tier = params.get("move_stop_to_breakeven_after_tier", True)
        # 賣出一半當下是否把剩餘部位的停損上移至進場成本價(保本)，False代表停損留在原本
        # 的結構停損位置不動。

        close = bars["close"]
        rsi_value = rsi(close, rsi_period)

        # shift(1)：不含當天，跟atr_breakout的唐奇安通道一樣避免look-ahead(當天的低點
        # 本身不該拿來當「跌破前N日低點」的判斷基準)。
        rolling_low_price = close.rolling(window=lookback_days).min().shift(1)
        rolling_low_rsi = rsi_value.rolling(window=lookback_days).min().shift(1)

        makes_new_low = close <= rolling_low_price
        rsi_diverges = rsi_value > rolling_low_rsi
        not_too_strong = rsi_value < rsi_ceiling  # 還在偏弱區間，避免已經強勢反彈完才進場

        entry_condition = makes_new_low & rsi_diverges & not_too_strong

        if require_macd_turn:
            _, _, histogram = macd(close)
            entry_condition = entry_condition & (histogram > histogram.shift(1))
        if require_kd_bullish:
            k, d = stochastic_kd(bars["high"], bars["low"], close)
            entry_condition = entry_condition & (k > d)
        if require_capitulation_volume:
            avg_vol_capitulation = rolling_avg_volume(bars["volume"], capitulation_volume_avg_period)
            entry_condition = entry_condition & (bars["volume"] > capitulation_volume_multiplier * avg_vol_capitulation)
        if require_long_uptrend_intact:
            long_trend_ok = sma(close, long_trend_ma_period).diff(long_trend_slope_lookback) > 0
            entry_condition = entry_condition & long_trend_ok

        prev_entry = entry_condition.shift(1).fillna(False).astype(bool)
        entry_edge = entry_condition & ~prev_entry

        if confirm_next_day:
            raw_signal = entry_edge.shift(1).fillna(False).astype(bool)
            entry_edge = raw_signal & (close > close.shift(1)) & (bars["low"] >= bars["low"].shift(1))

        if require_reversal_confirm:
            confirm_ma = close.rolling(reversal_confirm_ma_period).mean()
            price_confirms = (close > confirm_ma) | (close > bars["high"].shift(1))
            confirm_signals = [price_confirms]
            if require_reversal_kd:
                confirm_k, confirm_d = stochastic_kd(bars["high"], bars["low"], close)
                confirm_signals.append(confirm_k > confirm_d)
            if require_reversal_macd:
                _, _, confirm_histogram = macd(close)
                rising = confirm_histogram > confirm_histogram.shift(1)
                macd_confirms = rising.copy()
                for streak_shift in range(1, reversal_confirm_macd_streak_days):
                    macd_confirms = macd_confirms & rising.shift(streak_shift).fillna(False)
                if reversal_confirm_macd_positive:
                    macd_confirms = macd_confirms & (confirm_histogram > 0)
                confirm_signals.append(macd_confirms)
            if require_reversal_volume:
                avg_vol_reversal = rolling_avg_volume(bars["volume"], reversal_volume_avg_period)
                confirm_signals.append(bars["volume"] > reversal_volume_multiplier * avg_vol_reversal)
            signal_count = sum(s.fillna(False).astype(int) for s in confirm_signals)
            confirmed_ok = (signal_count >= reversal_confirm_min_signals).to_numpy()

            # 逐日掃描：創新低+背離當天只記下「等待確認」的狀態，不直接進場；接下來每天
            # 檢查確認條件有沒有出現，出現才算entry_edge，超過reversal_confirm_max_wait_days
            # 天還沒確認就放棄這次訊號。如果等待期間又出現新的創新低+背離(更低的低點)，
            # 用新的訊號日重新起算等待——新低點才是現在真正的參考基準。
            raw_signal = entry_edge.to_numpy()
            confirmed_edge = [False] * len(raw_signal)
            pending_idx = None
            for i in range(len(raw_signal)):
                if raw_signal[i]:
                    pending_idx = i
                    continue
                if pending_idx is None:
                    continue
                if i - pending_idx > reversal_confirm_max_wait_days:
                    pending_idx = None
                elif confirmed_ok[i]:
                    confirmed_edge[i] = True
                    pending_idx = None
            entry_edge = pd.Series(confirmed_edge, index=bars.index)

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
                elif entry_edge[t] and not pd.isna(rolling_low_rsi[t]):
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
                            f"價格創{lookback_days}日新低但RSI({rsi_period})未破底(背離)，"
                            f"分批停損{stop_pct_half * 100:.0f}%/{stop_pct_full * 100:.0f}%",
                        )
                    )

            return events

        if stop_mode == "structural" and enable_tiered_profit:
            ma_tiered = sma(close, tiered_ma_period)
            events: list[SignalEvent] = []
            in_position = False
            half_sold = False
            entry_price = None
            stop = None
            peak = None

            for t in bars.index:
                c = close[t]
                if in_position:
                    if not half_sold:
                        if c < stop:
                            events.append(
                                SignalEvent(symbol, self.name, Direction.SELL, c, t, f"跌破結構停損 {stop:.2f}(背離失敗，全部出場)")
                            )
                            in_position = False
                            entry_price = None
                            stop = None
                        else:
                            profit_hit = (c - entry_price) / entry_price >= tiered_target_pct
                            ma_hit = not pd.isna(ma_tiered[t]) and c >= ma_tiered[t]
                            if profit_hit or ma_hit:
                                reason = (
                                    f"獲利達{tiered_target_pct * 100:.0f}%" if profit_hit else f"觸及{tiered_ma_period}日均線"
                                )
                                events.append(SignalEvent(symbol, self.name, Direction.SELL, c, t, f"{reason}，賣出一半"))
                                half_sold = True
                                if move_stop_to_breakeven_after_tier:
                                    stop = entry_price
                                peak = c
                    else:
                        if c < stop:
                            label = "保本停損" if stop == entry_price else f"{stop_pct * 100:.0f}%移動停損"
                            events.append(
                                SignalEvent(symbol, self.name, Direction.SELL, c, t, f"跌破{label} {stop:.2f}，賣出剩餘一半")
                            )
                            in_position = False
                            half_sold = False
                            entry_price = None
                            stop = None
                            peak = None
                        else:
                            peak = max(peak, c)
                            stop = max(stop, peak * (1 - stop_pct))
                elif entry_edge[t] and not pd.isna(rolling_low_rsi[t]):
                    entry_price = c
                    stop = bars["low"][t] * (1 - structural_stop_buffer_pct)
                    half_sold = False
                    peak = c
                    events.append(
                        SignalEvent(
                            symbol,
                            self.name,
                            Direction.BUY,
                            c,
                            t,
                            f"價格創{lookback_days}日新低但RSI({rsi_period})未破底(背離)，結構停損{stop:.2f}"
                            f"(獲利達{tiered_target_pct * 100:.0f}%或觸及{tiered_ma_period}日均線先賣一半)",
                        )
                    )
                    in_position = True

            return events

        atr_value = atr(bars["high"], bars["low"], close, atr_period)

        def next_stop(c: float, t) -> float:
            if stop_mode == "pct":
                return c * (1 - stop_pct)
            if stop_mode == "structural":
                return bars["low"][t] * (1 - structural_stop_buffer_pct)
            return c - atr_multiplier * atr_value[t]

        if stop_mode == "pct":
            stop_label = f"{stop_pct * 100:.0f}%移動停損"
        elif stop_mode == "structural":
            stop_label = f"結構停損(進場K棒低點-{structural_stop_buffer_pct * 100:.0f}%)"
        else:
            stop_label = "ATR移動停損"

        events: list[SignalEvent] = []
        in_position = False
        stop = None
        entry_price = None
        trailing_now = False  # structural_trail_after_pct啟用後，獲利切換成移動停損時設True

        for t in bars.index:
            c = close[t]
            if in_position:
                if (
                    stop_mode == "structural"
                    and structural_trail_after_pct is not None
                    and not trailing_now
                    and (c - entry_price) / entry_price >= structural_trail_after_pct
                ):
                    trailing_now = True
                    stop = max(stop, c * (1 - stop_pct))

                if c < stop:
                    exit_label = f"{stop_pct * 100:.0f}%移動停損(獲利後切換)" if trailing_now else stop_label
                    events.append(SignalEvent(symbol, self.name, Direction.SELL, c, t, f"跌破{exit_label} {stop:.2f}"))
                    in_position = False
                    stop = None
                    entry_price = None
                    trailing_now = False
                elif trailing_now:
                    stop = max(stop, c * (1 - stop_pct))
                elif stop_mode == "pct" or (stop_mode == "atr" and not pd.isna(atr_value[t])):
                    # structural停損進場後固定不動(保護的是進場當下的結構是否還成立，不是
                    # 拿來鎖定後續獲利用的移動停損)，所以這裡刻意不幫structural往上移動——
                    # 除非structural_trail_after_pct已觸發(trailing_now，上面已處理)。
                    stop = max(stop, next_stop(c, t))
            elif (
                entry_edge[t]
                and not pd.isna(rolling_low_rsi[t])
                and (stop_mode in ("pct", "structural") or not pd.isna(atr_value[t]))
            ):
                stop = next_stop(c, t)
                entry_price = c
                trailing_now = False
                events.append(
                    SignalEvent(
                        symbol,
                        self.name,
                        Direction.BUY,
                        c,
                        t,
                        f"價格創{lookback_days}日新低但RSI({rsi_period})未破底(背離)，{stop_label} {stop:.2f}",
                    )
                )
                in_position = True

        return events
