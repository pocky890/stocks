import pandas as pd

from stocks.indicators import atr, rsi
from stocks.models import Direction, SignalEvent


class ChipMomentumStrategy:
    """外資連續買超為主訊號、RSI避免追高當濾網——用觀察清單7檔股票3年資料驗證過：
    2330/2454/2408/3450上都有>50%勝率+正報酬，不是只對單一股票過度配適，但3189不適用，
    8299/5439因為上櫃籌碼歷史資料不足還無法驗證(見chip_momentum相關分析)。
    出場預設用固定15%移動停損(stop_mode="pct")，也支援2.5倍ATR移動停損(stop_mode=
    "atr")——2026-08-15用scripts/backtest_stop_comparison.py全觀察清單10年回測比較過：
    這支策略沒有其他出場條件、只靠停損，改成固定15%後平均報酬/加總報酬/獲利因子全面
    提升(獲利因子2.54→3.22)，才改成15%當預設(ATR倍數2.5倍的由來：2倍在高波動股上
    太容易被正常回檔洗出去，3倍雖然平均報酬更高但幾乎全靠少數幾筆極端波段撐起來，
    2.5倍是當初驗證後比較平衡的取捨，stop_mode="atr"時仍沿用)。沒有foreign_net欄位
    (bars沒join到institutional_flows)就直接跳過，跟institutional_streak一樣的防護。"""

    name = "chip_momentum"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        if "foreign_net" not in bars.columns:
            return []

        chip_streak_days = params.get("chip_streak_days", 3)
        rsi_period = params.get("rsi_period", 14)
        rsi_overbought = params.get("rsi_overbought", 70)
        atr_period = params.get("atr_period", 14)
        atr_multiplier = params.get("atr_multiplier", 2.5)
        stop_mode = params.get("stop_mode", "pct")  # "atr"、"pct" 或 "tiered_pct"
        stop_pct = params.get("stop_pct", 0.15)
        stop_pct_half = params.get("stop_pct_half", 0.08)
        stop_pct_full = params.get("stop_pct_full", 0.15)
        entry_mode = params.get("entry_mode", "streak")  # "streak"(現行:連續剛好chip_streak_days
        # 天買超) 或 "window"(2026-08-16研究中：近cum_window_days日外資買超累積淨額為正，
        # 且近recent_window_days日內至少recent_min_buy_days天買超——跟trust_momentum的
        # 視窗概念類似，但這裡刻意用兩層(長窗口看累積方向+短窗口看最近有沒有持續買，
        # 不是trust_momentum那種單一視窗)，比原本「連續剛好3天」寬鬆，容許中間偶爾斷一天
        # 不買。預設仍是streak，不影響既有行為。
        cum_window_days = params.get("cum_window_days", 10)
        recent_window_days = params.get("recent_window_days", 3)
        recent_min_buy_days = params.get("recent_min_buy_days", 2)

        close = bars["close"]
        foreign_net = bars["foreign_net"].fillna(0)

        if entry_mode == "window":
            cum_positive = foreign_net.rolling(cum_window_days).sum() > 0
            recent_buy_days = (foreign_net > 0).rolling(recent_window_days).sum()
            foreign_buy_streak = cum_positive & (recent_buy_days >= recent_min_buy_days)
        else:
            sign = foreign_net.apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
            group_id = (sign != sign.shift()).cumsum()
            streak = sign.groupby(group_id).cumcount() + 1
            foreign_buy_streak = (sign == 1) & (streak == chip_streak_days)

        not_overbought = rsi(close, rsi_period) < rsi_overbought
        entry_condition = foreign_buy_streak & not_overbought
        prev_entry = entry_condition.shift(1).fillna(False).astype(bool)
        entry_edge = entry_condition & ~prev_entry

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
