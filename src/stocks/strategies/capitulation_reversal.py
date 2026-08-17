import pandas as pd

from stocks.indicators import atr, rolling_avg_volume, sma
from stocks.models import Direction, SignalEvent


class CapitulationReversalStrategy:
    """爆量急殺止穩策略。

    進場：單日重挫≥5% + 成交量>2倍均量(恐慌性賣壓)，隔天不再破前一天低點、且收盤收在
    前一天收盤之上(止穩確認)才進場。

    出場(現行:stop_mode="structural"+enable_tiered_profit，一買配兩賣，跟bullish_
    divergence同一套機制，要用simulate_scaleout_trades配對，不能套simulate_
    round_trips)，兩階段：
      ①初始結構停損防接刀：爆量急殺當天(不是進場當天)的最低點再往下5%緩衝
        (structural_stop_buffer_pct)
      ②反彈觸及20日均線(tiered_ma_period)先賣一半，剩餘部位停損上移至成本價保本
        (move_stop_to_breakeven_after_tier)，之後改用15%(stop_pct)寬幅移動停損

    也支援單一停損：固定15%移動停損("pct")、2.5倍ATR移動停損("atr")、分批停損
    ("tiered_pct")、或不搭配tiered_profit的純結構停損("structural"，注意：固定不動
    又沒有其他出場條件，獲利部位會一直持有到觸及停損為止，見enable_tiered_profit
    參數註解的實測說明)。

    現行(config.json):loss_cooldown_days=180——上一次進場如果真的觸發「跌破結構停損、
    恐慌未止穩、全部出場」(代表這支股票的恐慌沒有真的止穩)，對這支股票暫停180天再重新
    進場；正常止穩獲利出場則完全不受影響。這是2026-08-16取代原本考慮過的120MA斜率
    濾網(require_long_uptrend_intact，全面性擋掉「120MA沒有上揚」的股票，全觀察清單
    總報酬會砍71%)的精準版方案：只針對「這支股票自己」反覆抄底失敗才冷卻，20支已知
    近年下跌很兇的股票上獲利因子從0.65拉到0.94(接近打平)，但全觀察清單10年總報酬只
    犧牲7%(獲利因子甚至微幅變好)，見scripts/backtest_capitulation_loss_cooldown.py。

    斷路器：豁免（在CIRCUIT_BREAKER_EXEMPT_STRATEGIES清單內）。查過全觀察清單10年138次
    BUY訊號：進場當天自己收盤價<20日均線的比例高達75.4%(單一股票急殺後本來就常常還在
    自己月線下方)，但「全市場同產業≥60%也跌破月線」這個AND條件只在3.6%的進場天成立
    (單一股票恐慌性急殺不代表整個產業同時系統性重挫)，兩者同時成立、真正會被斷路器
    擋下的比例只有2.2%——跟bullish_divergence那種「進場前提本身就跟斷路器條件結構
    互斥」不同，這2.2%不是結構性衝突。使用者2026-08-16仍選擇排除：這2.2%剛好是
    「單一股票恐慌急殺+整個產業同時系統性重挫」同時發生的情況，可能正是最劇烈、最
    值得抓的恐慌轉折點，寧可不設這道防線也不要錯過。"""

    name = "capitulation_reversal"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        drop_threshold_pct = params.get("drop_threshold_pct", -5.0)
        volume_multiplier = params.get("volume_multiplier", 2.0)
        avg_volume_period = params.get("avg_volume_period", 20)
        atr_period = params.get("atr_period", 14)
        atr_multiplier = params.get("atr_multiplier", 2.5)
        stop_mode = params.get("stop_mode", "pct")  # "atr"、"pct"、"tiered_pct" 或
        # "structural"(現行:爆量急殺當天最低點-緩衝%，固定不動，搭配enable_tiered_profit=
        # True形成兩階段出場，見class docstring)
        stop_pct = params.get("stop_pct", 0.15)
        stop_pct_half = params.get("stop_pct_half", 0.08)
        stop_pct_full = params.get("stop_pct_full", 0.15)
        structural_stop_buffer_pct = params.get("structural_stop_buffer_pct", 0.05)  # stop_mode=
        # "structural"時，停損=爆量急殺當天最低點再往下的緩衝百分比。
        enable_tiered_profit = params.get("enable_tiered_profit", False)  # stop_mode=
        # "structural"時額外啟用的兩階段出場架構(一買配兩賣，見class docstring)。現行
        # (config.json):True。用scripts/backtest_capitulation_reversal_user_proposal.py
        # 驗證過：跟trend_following/atr_breakout/long_swing那些「提早鎖利就系統性犧牲
        # 總報酬」的結論不同，這支策略啟用後獲利因子不降反升(5.82→7.62)、最大回撤大幅
        # 收斂(-110.1→-68.6)，代價是總報酬下滑(3863.0→1740.1)——原因可能是這支策略的
        # 真正優勢本來就是「快、狠、準地吃到反彈」而不是「抱住大波段」，跟trend_following
        # 那種regime-following策略的獲利結構不一樣。拆開逐檔看，有幾檔原本是正報酬
        # (2337/2408/3526/6491/6187/7769/8299)套用後變成負報酬，不是全面受益；使用者
        # 2026-08-16確認接受這個取捨，故採用為預設。只單純只改結構停損(不搭配
        # tiered_profit)一樣有bullish_divergence踩過的問題：固定不動又沒有其他出場
        # 條件，獲利部位永遠不出場，統計會失真(全觀察清單10年只剩53筆「完整」交易、
        # 且全部虧損)，不要單獨使用。
        tiered_ma_period = params.get("tiered_ma_period", 20)  # 賣出一半的觸發條件：收盤
        # 觸及/站上這條均線(反彈碰上方均線壓力最容易回檔)。現行(config.json):20。10日
        # 版本回測獲利因子更高(9.13)、MDD更小(-41.1)，但總報酬更低(1476.0)——20日是
        # 總報酬/獲利因子/MDD三者較平衡的版本，故採用。
        move_stop_to_breakeven_after_tier = params.get("move_stop_to_breakeven_after_tier", True)
        # 賣出一半當下是否把剩餘部位的停損上移至進場成本價(保本)，False代表停損留在原本
        # 的結構停損位置不動。
        require_long_uptrend_intact = params.get("require_long_uptrend_intact", False)  # 研究
        # 參數(2026-08-16使用者轉述Gemini建議)：額外要求long_trend_ma_period(現行:120)日
        # 均線斜率向上才准進場，跟bullish_divergence同一套「長線仍多頭、只是短線跌深」
        # 判斷，區分正常急殺反彈vs結構性空頭裡的死貓反彈。預設False，未啟用，見
        # scripts/backtest_macro_regime_filters.py。
        long_trend_ma_period = params.get("long_trend_ma_period", 120)
        long_trend_slope_lookback = params.get("long_trend_slope_lookback", 20)
        loss_cooldown_days = params.get("loss_cooldown_days", 0)  # 研究參數(2026-08-16)：
        # require_long_uptrend_intact是全面性的regime濾網，會連正常整理(120MA走平但不是
        # 結構性空頭)的健康股票也一起濾掉，全觀察清單10年總報酬砍71%代價太大。這個是更
        # 精準的替代方案：只有這支股票自己的上一次進場「真的觸發全部出場的結構停損」
        # (恐慌沒有真的止穩，代表這支股票可能還在結構性下跌)才進入冷卻期，不影響其他
        # 正常運作的股票、也不影響同一支股票下一次表現正常的抄底訊號。預設0(不啟用)，
        # 見scripts/backtest_capitulation_loss_cooldown.py。

        close = bars["close"]
        low = bars["low"]
        avg_vol = rolling_avg_volume(bars["volume"], avg_volume_period)

        daily_return_pct = close.pct_change() * 100
        is_capitulation = (daily_return_pct <= drop_threshold_pct) & (bars["volume"] > volume_multiplier * avg_vol)

        # 用shift(1)看「前一天是不是爆量急殺日」，今天再確認止穩(不破前低+收盤收高)——
        # 隔天才進場，不是急殺當天就搶進場，避免當天盤中還在探底就先接刀。
        prev_is_capitulation = is_capitulation.shift(1).fillna(False)
        prev_close = close.shift(1)
        prev_low = low.shift(1)
        confirms_reversal = prev_is_capitulation & (close > prev_close) & (low >= prev_low)
        if require_long_uptrend_intact:
            long_trend_ok = sma(close, long_trend_ma_period).diff(long_trend_slope_lookback) > 0
            confirms_reversal = confirms_reversal & long_trend_ok

        # 2026-08-17效能優化：series[t]是label-based lookup(DatetimeIndex.get_loc)，逐bar
        # 呼叫好幾次、乘上上千個bar，profiling量到佔了evaluate()總時間近8成——先轉成numpy
        # array用位置索引，邏輯完全不變，只是indexing方式改變。
        index = bars.index
        close_arr = close.to_numpy()
        confirms_reversal_arr = confirms_reversal.to_numpy()
        prev_low_arr = prev_low.to_numpy()

        if stop_mode == "tiered_pct":
            events: list[SignalEvent] = []
            in_position = False
            half_sold = False
            stop_half = None
            stop_full = None

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
                elif confirms_reversal_arr[i]:
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
                            f"前日重挫{drop_threshold_pct:.0f}%+爆量{volume_multiplier:.0f}倍後隔日止穩，"
                            f"分批停損{stop_pct_half * 100:.0f}%/{stop_pct_full * 100:.0f}%",
                        )
                    )

            return events

        if stop_mode == "structural" and enable_tiered_profit:
            ma_tiered = sma(close, tiered_ma_period)
            ma_tiered_arr = ma_tiered.to_numpy()
            events: list[SignalEvent] = []
            in_position = False
            half_sold = False
            entry_price = None
            stop = None
            peak = None
            cooldown_remaining = 0

            for i, t in enumerate(index):
                c = close_arr[i]
                if in_position:
                    if not half_sold:
                        if c < stop:
                            events.append(
                                SignalEvent(symbol, self.name, Direction.SELL, c, t, f"跌破結構停損 {stop:.2f}(恐慌未止穩，全部出場)")
                            )
                            in_position = False
                            entry_price = None
                            stop = None
                            cooldown_remaining = loss_cooldown_days
                        elif not pd.isna(ma_tiered_arr[i]) and c >= ma_tiered_arr[i]:
                            events.append(
                                SignalEvent(symbol, self.name, Direction.SELL, c, t, f"觸及{tiered_ma_period}日均線，賣出一半")
                            )
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
                elif cooldown_remaining > 0:
                    cooldown_remaining -= 1
                elif confirms_reversal_arr[i]:
                    entry_price = c
                    stop = prev_low_arr[i] * (1 - structural_stop_buffer_pct)
                    half_sold = False
                    peak = c
                    events.append(
                        SignalEvent(
                            symbol,
                            self.name,
                            Direction.BUY,
                            c,
                            t,
                            f"前日重挫{drop_threshold_pct:.0f}%+爆量{volume_multiplier:.0f}倍後隔日止穩，"
                            f"結構停損{stop:.2f}(觸及{tiered_ma_period}日均線先賣一半)",
                        )
                    )
                    in_position = True

            return events

        atr_value = atr(bars["high"], bars["low"], close, atr_period)
        atr_arr = atr_value.to_numpy()

        def next_stop(c: float, atr_val: float, prev_low_val: float) -> float:
            if stop_mode == "pct":
                return c * (1 - stop_pct)
            if stop_mode == "structural":
                return prev_low_val * (1 - structural_stop_buffer_pct)
            return c - atr_multiplier * atr_val

        if stop_mode == "pct":
            stop_label = f"{stop_pct * 100:.0f}%移動停損"
        elif stop_mode == "structural":
            stop_label = f"結構停損(急殺當天低點-{structural_stop_buffer_pct * 100:.0f}%)"
        else:
            stop_label = "ATR移動停損"

        events: list[SignalEvent] = []
        in_position = False
        stop = None

        for i, t in enumerate(index):
            c = close_arr[i]
            if in_position:
                if c < stop:
                    events.append(SignalEvent(symbol, self.name, Direction.SELL, c, t, f"跌破{stop_label} {stop:.2f}"))
                    in_position = False
                    stop = None
                elif stop_mode == "pct" or (stop_mode == "atr" and not pd.isna(atr_arr[i])):
                    # structural停損進場後固定不動(保護的是恐慌是否真的止穩，不是拿來
                    # 鎖定後續獲利用的移動停損)，所以這裡刻意不幫structural往上移動。
                    stop = max(stop, next_stop(c, atr_arr[i], prev_low_arr[i]))
            elif confirms_reversal_arr[i] and (stop_mode in ("pct", "structural") or not pd.isna(atr_arr[i])):
                stop = next_stop(c, atr_arr[i], prev_low_arr[i])
                events.append(
                    SignalEvent(
                        symbol,
                        self.name,
                        Direction.BUY,
                        c,
                        t,
                        f"前日重挫{drop_threshold_pct:.0f}%+爆量{volume_multiplier:.0f}倍後隔日止穩，{stop_label} {stop:.2f}",
                    )
                )
                in_position = True

        return events
