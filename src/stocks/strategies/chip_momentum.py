import pandas as pd

from stocks.indicators import atr, rsi
from stocks.models import Direction, SignalEvent


class ChipMomentumStrategy:
    """外資連續買超為主訊號、RSI避免追高當濾網、ATR移動停損出場——用觀察清單7檔股票3年
    資料驗證過：2330/2454/2408/3450上都有>50%勝率+正報酬，不是只對單一股票過度配適，但
    3189不適用，8299/5439因為上櫃籌碼歷史資料不足還無法驗證(見chip_momentum相關分析)。
    ATR倍數用2.5倍(不是atr_breakout的2倍)——2倍在高波動股上太容易被正常回檔洗出去，
    3倍雖然平均報酬更高但幾乎全靠少數幾筆極端波段撐起來，2.5倍是驗證後比較平衡的取捨。
    沒有foreign_net欄位(bars沒join到institutional_flows)就直接跳過，跟institutional_streak
    一樣的防護。"""

    name = "chip_momentum"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        if "foreign_net" not in bars.columns:
            return []

        chip_streak_days = params.get("chip_streak_days", 3)
        rsi_period = params.get("rsi_period", 14)
        rsi_overbought = params.get("rsi_overbought", 70)
        atr_period = params.get("atr_period", 14)
        atr_multiplier = params.get("atr_multiplier", 2.5)

        close = bars["close"]
        foreign_net = bars["foreign_net"].fillna(0)
        sign = foreign_net.apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
        group_id = (sign != sign.shift()).cumsum()
        streak = sign.groupby(group_id).cumcount() + 1
        foreign_buy_streak = (sign == 1) & (streak == chip_streak_days)

        not_overbought = rsi(close, rsi_period) < rsi_overbought
        entry_condition = foreign_buy_streak & not_overbought
        prev_entry = entry_condition.shift(1).fillna(False).astype(bool)
        entry_edge = entry_condition & ~prev_entry

        atr_value = atr(bars["high"], bars["low"], close, atr_period)

        events: list[SignalEvent] = []
        in_position = False
        stop = None

        for t in bars.index:
            c = close[t]
            if in_position:
                if c < stop:
                    events.append(SignalEvent(symbol, self.name, Direction.SELL, c, t, f"跌破ATR移動停損 {stop:.2f}"))
                    in_position = False
                    stop = None
                elif not pd.isna(atr_value[t]):
                    stop = max(stop, c - atr_multiplier * atr_value[t])
            elif entry_edge[t] and not pd.isna(atr_value[t]):
                stop = c - atr_multiplier * atr_value[t]
                events.append(
                    SignalEvent(symbol, self.name, Direction.BUY, c, t, f"外資連{chip_streak_days}日買超(未超買)，ATR停損 {stop:.2f}")
                )
                in_position = True

        return events
