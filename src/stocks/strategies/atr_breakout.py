import pandas as pd

from stocks.indicators import atr, weekly_trend_confirmed
from stocks.models import Direction, SignalEvent


class ATRBreakoutStrategy:
    """ATR動態通道突破策略。

    進場：收盤價創20日新高(唐奇安通道上軌) + 週線趨勢確認(require_weekly_trend：週MA20
    斜率向上)

    出場：跌破15%移動停損(stop_mode="pct")。停損只進不退：每天先用前一天算出的停損線
    判斷是否出場，沒出場才用當天收盤價把停損線往上拉，避免look-ahead。

    也支援N倍ATR移動停損(stop_mode="atr")、分批停損(stop_mode="tiered_pct")、雙重停損
    (stop_mode="two_stage"：進場時先用較窄的初始停損(進場K棒最低點或1.5倍ATR)，等獲利
    超過profit_switch_pct(預設10%)後才切換成15%移動停損)。

    斷路器：適用（名義上）——全市場同產業≥60%股票跌破月線(20日均線)、且這支股票自己
    當下也跌破月線時，暫停新的BUY。但這支策略的進場前提是收盤價創20日新高，實務上
    幾乎不可能同時跌破自己的20日均線——查過全觀察清單10年355次BUY訊號，進場當天收盤價
    低於自己20日均線的次數是0，也就是「自己也跌破月線」這個AND條件對這支策略從未成立過，
    斷路器對這支策略等於形同虛設，只在SELL/既有部位這端沒有影響（本來就不擋SELL）。"""

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

        close = bars["close"]
        donchian_upper = bars["high"].rolling(window=donchian_period).max().shift(1)
        entry_gate = pd.Series(True, index=bars.index)
        if require_weekly_trend:
            entry_gate = weekly_trend_confirmed(close, weekly_ma_period, require_slope_up=(weekly_trend_mode == "slope"))

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
