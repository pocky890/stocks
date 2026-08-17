import pandas as pd

from stocks.indicators import atr, rolling_avg_volume, sma
from stocks.models import Direction, SignalEvent


class TrendFollowingStrategy:
    """趨勢追蹤策略。

    進場條件：
    1. 20日均線>60日均線
    2. 收盤站上20日均線
    3. 成交量>20日均量(volume_multiplier，現行1倍)

    出場條件：
    1. 收盤跌破20日均線，或
    2. 20日均線跌破60日均線(多頭排列瓦解)
    停損為進場當天收盤價-2倍14日ATR，固定不動。

    支援模式(回測用)：
    - stop_mode="pct"：移動停損
    - stop_mode="trailing_atr"：移動停利(自進場後最高點回落N倍ATR出場)
    - ma_break_confirm_days/ma_break_single_day_drop_pct：跌破20日均線加緩衝確認
    - entry_trigger="level"：條件當天成立即觸發(非邊緣，已驗證等效)

    斷路器：ON — 全市場同產業≥60%跌破月線時暫停BUY(純看產業寬度)"""

    name = "trend_following"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        fast = params.get("fast", 20)
        slow = params.get("slow", 60)
        volume_avg_period = params.get("volume_avg_period", 20)
        volume_multiplier = params.get("volume_multiplier", 1.0)  # 現行:1.0(僅需>均量)，
        # 可調高至1.5/2.0要求更強的量能確認。用scripts/backtest_trend_following_user_proposal.py
        # 驗證過：調高後勝率/獲利因子/最大回撤都變好，但交易筆數大減、加總報酬明顯下滑
        # (10年7046.2→1.5倍6334.9→2倍5374.6)——用更少更精但更少的訊號換總報酬，是
        # 品質/總量的取捨，不是單純的改善，未採用為預設。
        atr_period = params.get("atr_period", 14)
        atr_multiplier = params.get("atr_multiplier", 2)
        stop_mode = params.get("stop_mode", "atr")  # "atr"(現行:進場後固定不動) 或
        # "pct"(移動停損) 或 "trailing_atr"(移動停利：股價自進場後最高點回落
        # trailing_atr_multiplier倍ATR即出場)——用scripts/backtest_trend_following_user_proposal.py
        # 驗證過trailing_atr是負面調整：10年加總報酬7046.2→5366.6(-24%)、獲利因子2.71→2.15，
        # 提前鎖利會系統性砍掉這支策略靠少數大波段撐報酬的真正獲利來源，跟long_swing
        # docstring記錄過的同一個結論一樣，未採用為預設。
        stop_pct = params.get("stop_pct", 0.15)
        trailing_atr_multiplier = params.get("trailing_atr_multiplier", 1.5)
        ma_break_confirm_days = params.get("ma_break_confirm_days", 1)  # 現行:1(當天跌破
        # 就算出場)，可調高要求連續N天收盤跌破20日均線才確認，過濾單日假跌破雜訊。
        ma_break_single_day_drop_pct = params.get("ma_break_single_day_drop_pct", None)  # 即使
        # 還沒滿ma_break_confirm_days天，只要單日跌幅(%)達到這個負值就立刻確認出場
        # (例如-3.0代表單日跌超3%直接算數)。現行None代表不啟用這個豁免。用
        # scripts/backtest_trend_following_user_proposal.py驗證過(連2天確認+單日3%豁免)：
        # 10年加總報酬小幅轉正(7046.2→7243.1)但2026 YTD打平、7月轉差，效果不穩定，
        # 也還沒有跟其他策略一起全面驗證過，暫不採用為預設，保留參數供之後測試用。
        entry_trigger = params.get("entry_trigger", "edge")  # "edge"(現行:條件剛從False轉
        # True那天才觸發) 或 "level"(條件當天成立就觸發，不要求邊緣)——已用
        # scripts/backtest_trend_following_entry_trigger.py驗證過是no-op(加總報酬幾乎沒差)，
        # 不需要改成level，保留參數供其他情境測試用。

        close = bars["close"]
        ma_fast = sma(close, fast)
        ma_slow = sma(close, slow)
        avg_volume = rolling_avg_volume(bars["volume"], volume_avg_period)
        atr_value = atr(bars["high"], bars["low"], close, atr_period)

        entry_condition = (ma_fast > ma_slow) & (close > ma_fast) & (bars["volume"] > volume_multiplier * avg_volume)
        if entry_trigger == "level":
            entry_edge = entry_condition
        else:
            prev_entry = entry_condition.shift(1).fillna(False).astype(bool)
            entry_edge = entry_condition & ~prev_entry

        below_fast = close < ma_fast
        group_id = (below_fast != below_fast.shift()).cumsum()
        below_streak = below_fast.groupby(group_id).cumcount() + 1
        streak_confirmed = below_fast & (below_streak >= ma_break_confirm_days)
        if ma_break_single_day_drop_pct is not None:
            daily_return_pct = close.pct_change() * 100
            single_day_break = below_fast & (daily_return_pct <= ma_break_single_day_drop_pct)
        else:
            single_day_break = pd.Series(False, index=bars.index)

        def next_stop(c: float, atr_val: float) -> float:
            if stop_mode == "pct":
                return c * (1 - stop_pct)
            if stop_mode == "trailing_atr":
                return c - trailing_atr_multiplier * atr_val
            return c - atr_multiplier * atr_val

        if stop_mode == "pct":
            stop_label = f"{stop_pct * 100:.0f}%移動停損"
        elif stop_mode == "trailing_atr":
            stop_label = f"{trailing_atr_multiplier}倍ATR移動停利"
        else:
            stop_label = "停損"

        # 2026-08-17效能優化：series[t]是label-based lookup(DatetimeIndex.get_loc)，逐bar
        # 呼叫好幾次、乘上上千個bar，profiling量到佔了evaluate()總時間近8成——先轉成numpy
        # array用位置索引，邏輯完全不變，只是indexing方式改變。
        index = bars.index
        close_arr = close.to_numpy()
        streak_confirmed_arr = streak_confirmed.to_numpy()
        single_day_break_arr = single_day_break.to_numpy()
        ma_fast_arr = ma_fast.to_numpy()
        ma_slow_arr = ma_slow.to_numpy()
        atr_arr = atr_value.to_numpy()
        entry_edge_arr = entry_edge.to_numpy()

        events: list[SignalEvent] = []
        in_position = False
        stop = None
        peak = None

        for i, t in enumerate(index):
            c = close_arr[i]
            if in_position:
                reasons = []
                if c < stop:
                    reasons.append(f"跌破{stop_label}{stop:.2f}")
                if streak_confirmed_arr[i]:
                    reasons.append(
                        f"連續{ma_break_confirm_days}天跌破{fast}日均線" if ma_break_confirm_days > 1 else f"跌破{fast}日均線"
                    )
                elif single_day_break_arr[i]:
                    reasons.append(f"單日跌破{fast}日均線且跌幅達{abs(ma_break_single_day_drop_pct):.0f}%")
                if ma_fast_arr[i] < ma_slow_arr[i]:
                    reasons.append(f"{fast}日均線跌破{slow}日均線")
                if reasons:
                    events.append(SignalEvent(symbol, self.name, Direction.SELL, c, t, "、".join(reasons)))
                    in_position = False
                    stop = None
                    peak = None
                elif stop_mode == "pct":
                    stop = max(stop, next_stop(c, atr_arr[i]))
                elif stop_mode == "trailing_atr" and not pd.isna(atr_arr[i]):
                    peak = max(peak, c)
                    stop = max(stop, peak - trailing_atr_multiplier * atr_arr[i])
            elif entry_edge_arr[i] and (stop_mode == "pct" or not pd.isna(atr_arr[i])):
                stop = next_stop(c, atr_arr[i])
                peak = c
                events.append(
                    SignalEvent(symbol, self.name, Direction.BUY, c, t, f"站上{fast}日均線且{fast}>{slow}日均線+爆量，{stop_label}{stop:.2f}")
                )
                in_position = True

        return events
