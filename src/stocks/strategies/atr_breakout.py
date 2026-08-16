import pandas as pd

from stocks.indicators import atr, rolling_avg_volume, sma, weekly_trend_confirmed
from stocks.models import Direction, SignalEvent


class ATRBreakoutStrategy:
    """ATR動態通道突破策略。

    進場：收盤價創20日新高(唐奇安通道上軌) + 週線趨勢確認(require_weekly_trend：週MA20
    斜率向上) + 60日均線>120日均線(require_long_regime，現行:True) + 收盤價>240日均線/
    年線(require_above_long_ma，現行:True，跟regime疊加、不是取代) + 月營收年增率>=0%
    (require_revenue_growth，現行:True，2026-08-16加的基本面濾網，見db.attach_
    monthly_revenue_growth())。這三道濾網都是2026-08-16加的：見scripts/backtest_
    long_regime_filter.py/backtest_macro_regime_filters.py/backtest_revenue_growth_
    filter.py，全觀察清單10年獲利因子3.10→4.06(疊加後)、最大回撤收斂約15%，20支已知
    近年下跌很兇的股票上獲利因子0.89→1.01(轉正)。

    出場：跌破25%移動停損(stop_mode="pct"，現行:0.25，2026-08-16從15%拉寬)。停損只進
    不退：每天先用前一天算出的停損線判斷是否出場，沒出場才用當天收盤價把停損線往上拉，
    避免look-ahead。拉寬理由：使用者提議「進場濾網加這麼多(regime/MA240/營收)了，訊號
    應該更精準，出場能不能放寬換更高報酬」，實測(scripts/backtest_wider_exit_stops.py)
    全觀察清單10年15%→20%→25%是乾淨的贏(加總報酬4242→5578→8057、獲利因子3.82→4.23→
    5.40同步變好，不是單純放大波動)，YTD樣本太小(僅8~4筆)看不出穩定方向，採用25%。

    也支援N倍ATR移動停損(stop_mode="atr")、分批停損(stop_mode="tiered_pct")、雙重停損
    (stop_mode="two_stage"：進場時先用較窄的初始停損(進場K棒最低點或1.5倍ATR)，等獲利
    超過profit_switch_pct(預設10%)後才切換成stop_pct寬幅移動停損)。

    斷路器：適用——全市場同產業≥60%股票跌破月線(20日均線)時暫停新的BUY。2026-08-16
    之前還要求「這支股票自己當下也跌破月線」才擋(AND條件)，但這支策略的進場前提是
    創20日新高，實務上幾乎不可能同時跌破自己的月線，AND條件對這支策略形同虛設
    (10年355次BUY訊號、擋下率0%)。使用者確認拿掉AND條件、改成純看產業寬度(config.
    circuit_breaker_own_ma_period=None)，見circuit_breaker.py開頭說明。"""

    name = "atr_breakout"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        donchian_period = params.get("donchian_period", 20)
        atr_period = params.get("atr_period", 14)
        atr_multiplier = params.get("atr_multiplier", 2)
        stop_mode = params.get("stop_mode", "pct")  # "atr"、"pct"(現行:固定15%移動停損)、
        # "tiered_pct"(分批停損) 或 "two_stage"(雙重停損：初始窄停損，獲利夠多後切換成
        # 寬幅移動停損)——用scripts/backtest_atr_breakout_stop_proposal.py驗證過："atr"
        # 2倍/2.5倍、"two_stage"兩種初始停損寫法，全觀察清單10年報酬/勝率/獲利因子全部
        # 輸給固定15%(加總報酬6368.7→3724.8~5721.3，勝率52.3%→32~46%)。原因是這個
        # 觀察清單以中大型成長股為主，2~2.5倍ATR對這些股票反而比15%更緊，導致正常回檔
        # 就被洗出場、錯過後續大波段——初始窄停損(two_stage)也是同樣道理：假突破雖然
        # 少賠一點，但真突破也常常還沒撐到獲利10%可以切寬幅停損就先被洗掉，跟這個策略
        # 靠少數大波段撐報酬的特性衝突，未採用為預設。
        stop_pct = params.get("stop_pct", 0.15)
        stop_pct_half = params.get("stop_pct_half", 0.08)
        stop_pct_full = params.get("stop_pct_full", 0.15)
        initial_stop_basis = params.get("initial_stop_basis", "atr")  # two_stage專用："atr"
        # (進場價-initial_stop_atr_multiplier倍ATR) 或 "bar_low"(進場K棒當天最低點)
        initial_stop_atr_multiplier = params.get("initial_stop_atr_multiplier", 1.5)
        profit_switch_pct = params.get("profit_switch_pct", 0.10)  # two_stage專用：獲利達到
        # 這個百分比才從初始窄停損切換成stop_pct(現行15%)寬幅移動停損
        require_weekly_trend = params.get("require_weekly_trend", False)  # 現行:True，額外
        # 要求週線級別的趨勢確認，過濾日線假突破。
        weekly_trend_mode = params.get("weekly_trend_mode", "slope")  # "slope"(現行:週MA本身
        # 斜率向上) 或 "above_ma"(收盤站上週MA)，見indicators.weekly_trend_confirmed。
        weekly_ma_period = params.get("weekly_ma_period", 20)
        require_entry_volume = params.get("require_entry_volume", False)  # 研究參數(2026-08-16
        # 使用者提議)：突破當天額外要求成交量>volume_multiplier倍均量，跟姊妹策略breakout
        # (唐奇安通道突破+1.5倍均量)同一套命名，過濾量縮盤整後的假突破新高。預設False，
        # 未啟用，見scripts/backtest_atr_breakout_volume_filter.py。
        volume_avg_period = params.get("volume_avg_period", 20)
        volume_multiplier = params.get("volume_multiplier", 1.5)
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
        donchian_upper = bars["high"].rolling(window=donchian_period).max().shift(1)
        entry_gate = pd.Series(True, index=bars.index)
        if require_weekly_trend:
            entry_gate = weekly_trend_confirmed(close, weekly_ma_period, require_slope_up=(weekly_trend_mode == "slope"))
        if require_above_long_ma:
            entry_gate = entry_gate & (close > sma(close, long_ma_period))
        if require_entry_volume:
            avg_vol = rolling_avg_volume(bars["volume"], volume_avg_period)
            entry_gate = entry_gate & (bars["volume"] > volume_multiplier * avg_vol)
        if require_long_regime:
            regime_active = sma(close, regime_fast_period) > sma(close, regime_slow_period)
            entry_gate = entry_gate & regime_active
        if require_revenue_growth:
            revenue_growth = bars.get("revenue_yoy_growth", pd.Series(float("nan"), index=bars.index))
            growth_ok = (revenue_growth >= revenue_growth_min_pct) | revenue_growth.isna()
            entry_gate = entry_gate & growth_ok

        if stop_mode == "tiered_pct":
            events: list[SignalEvent] = []
            in_position = False
            half_sold = False
            stop_half = None
            stop_full = None

            for t in bars.index:
                if pd.isna(donchian_upper[t]):
                    continue
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
                elif c > donchian_upper[t] and entry_gate[t]:
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
                            f"創{donchian_period}日新高突破，分批停損{stop_pct_half * 100:.0f}%/{stop_pct_full * 100:.0f}%",
                        )
                    )

            return events

        atr_value = atr(bars["high"], bars["low"], close, atr_period)

        if stop_mode == "two_stage":
            events: list[SignalEvent] = []
            in_position = False
            entry_price = None
            stage = None  # "tight" 或 "wide"
            stop = None

            for t in bars.index:
                if pd.isna(donchian_upper[t]) or pd.isna(atr_value[t]):
                    continue
                c = close[t]

                if in_position:
                    if stage == "tight" and (c - entry_price) / entry_price >= profit_switch_pct:
                        stage = "wide"
                        stop = c * (1 - stop_pct)

                    if c < stop:
                        label = "初始窄停損" if stage == "tight" else f"{stop_pct * 100:.0f}%寬幅移動停損"
                        events.append(SignalEvent(symbol, self.name, Direction.SELL, c, t, f"跌破{label} {stop:.2f}"))
                        in_position = False
                        entry_price = None
                        stage = None
                        stop = None
                    elif stage == "wide":
                        stop = max(stop, c * (1 - stop_pct))
                elif c > donchian_upper[t] and entry_gate[t]:
                    entry_price = c
                    stop = bars["low"][t] if initial_stop_basis == "bar_low" else c - initial_stop_atr_multiplier * atr_value[t]
                    stage = "tight"
                    in_position = True
                    events.append(
                        SignalEvent(
                            symbol,
                            self.name,
                            Direction.BUY,
                            c,
                            t,
                            f"創{donchian_period}日新高突破，初始窄停損{stop:.2f}(獲利達{profit_switch_pct * 100:.0f}%後"
                            f"切換{stop_pct * 100:.0f}%寬幅移動停損)",
                        )
                    )

            return events

        def next_stop(c: float, t) -> float:
            if stop_mode == "pct":
                return c * (1 - stop_pct)
            return c - atr_multiplier * atr_value[t]

        stop_label = f"{stop_pct * 100:.0f}%移動停損" if stop_mode == "pct" else "ATR移動停損"

        events: list[SignalEvent] = []
        in_position = False
        stop = None

        for t in bars.index:
            if pd.isna(donchian_upper[t]) or (stop_mode == "atr" and pd.isna(atr_value[t])):
                continue
            c = close[t]

            if in_position:
                if c < stop:
                    events.append(
                        SignalEvent(symbol, self.name, Direction.SELL, c, t, f"跌破{stop_label} {stop:.2f}")
                    )
                    in_position = False
                    stop = None
                else:
                    stop = max(stop, next_stop(c, t))
            elif c > donchian_upper[t] and entry_gate[t]:
                stop = next_stop(c, t)
                events.append(
                    SignalEvent(symbol, self.name, Direction.BUY, c, t, f"創{donchian_period}日新高突破，{stop_label} {stop:.2f}")
                )
                in_position = True

        return events
