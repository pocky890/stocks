import pandas as pd

from stocks.indicators import atr, rsi, sma
from stocks.models import Direction, SignalEvent


class TrustMomentumStrategy:
    """跟chip_momentum同一套邏輯，主訊號換成投信(trust_net)買超——投信對中小型股的訊號
    通常比外資更敏感(外資很多時候是被動跟指數走，投信才是主動選股)，用同樣的RSI濾網，
    方便跟chip_momentum直接對照哪個籌碼來源在哪支股票上更好用。出場預設用固定15%移動
    停損(stop_mode="pct")，也支援2.5倍ATR移動停損(stop_mode="atr")——2026-08-15用
    scripts/backtest_stop_comparison.py全觀察清單10年回測比較過，這支策略沒有其他出場
    條件、只靠停損，改成固定15%後平均報酬/加總報酬/獲利因子全面提升(獲利因子2.30→3.01，
    連最大回撤都變淺)，才改成15%當預設。沒有trust_net欄位就直接跳過，跟chip_momentum
    一樣的防護。

    主訊號條件2026-08-08調整：從「連續N天買超」改成「近chip_window天內有至少
    chip_min_buy_days天買超、且淨額加總為正」——連續買超太剛性，投信「買、觀望、再買」
    這種偶爾斷一天的強勢股會被濾掉。這個調整是使用者提出、經過全觀察清單3年回測驗證
    後才採用(不是憑邏輯猜的)：投信這組換成新條件後勝率(44.4%→45.0%)、平均報酬
    (+7.17%→+7.42%)、加總報酬(889.4→972.0)三個指標同時變好；同樣的調整套在chip_momentum
    (外資)上測試則是平均跟勝率雙雙變差，所以chip_momentum維持原本的「連續N天」條件不動——
    兩個策略雖然邏輯相似，最佳參數不必然一樣，以各自回測結果為準。

    進場改成level-triggered(2026-08-08)：trust_buy_streak用">="不是"=="，投信買超動能
    夠強時可以連續多天維持True，原本edge_trigger(entry_condition & ~prev_entry)只認
    「剛從False轉True」那一天，會導致停損出場後只要條件還沒轉一輪False→True就卡住進不了
    場，錯過停損後的續漲——跟long_swing一開始踩過的同一種bug。經全觀察清單回測驗證：
    加總報酬+11.4%(1622.2→1807.7)，12檔裡9檔變好或持平，只有3105/2344小幅變差，
    平均報酬幾乎沒變(+9.11%→+8.65%)、勝率小跌(49.4%→47.4%)但趨勢跟隨策略不該用勝率
    否決。同樣的分析用在chip_momentum上發現是no-op：chip_momentum的foreign_buy_streak
    用"=="(剛好等於chip_streak_days)，本來就是單日脈衝條件，不可能連續兩天為True，
    edge/level兩種寫法回測結果完全一樣，所以chip_momentum維持原本寫法不變。"""

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
        stop_mode = params.get("stop_mode", "pct")  # "atr"、"pct" 或 "tiered_pct"
        stop_pct = params.get("stop_pct", 0.15)
        stop_pct_half = params.get("stop_pct_half", 0.08)
        stop_pct_full = params.get("stop_pct_full", 0.15)
        require_uptrend = params.get("require_uptrend", False)  # 2026-08-15研究中：擋掉
        # 大盤/個股趨勢已經轉弱、但投信當下還在買的假訊號(2026年7月那波系統性重挫期間
        # 全觀察清單同時觸發、同時停損就是這種情況)——預設False不影響既有行為，只有
        # 明確帶這個參數做回測比較時才會生效，跟long_swing的MA60>MA120 regime概念同源。
        trend_ma_period = params.get("trend_ma_period", 60)

        close = bars["close"]
        trust_net = bars["trust_net"].fillna(0)
        buy_days_in_window = (trust_net > 0).rolling(window=chip_window_days).sum()
        net_sum_in_window = trust_net.rolling(window=chip_window_days).sum()
        trust_buy_streak = (buy_days_in_window >= chip_min_buy_days) & (net_sum_in_window > 0)

        not_overbought = rsi(close, rsi_period) < rsi_overbought
        entry_condition = trust_buy_streak & not_overbought
        if require_uptrend:
            entry_condition = entry_condition & (close > sma(close, trend_ma_period))

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
                elif entry_condition[t]:
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
            elif entry_condition[t] and (stop_mode == "pct" or not pd.isna(atr_value[t])):
                stop = next_stop(c, t)
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
