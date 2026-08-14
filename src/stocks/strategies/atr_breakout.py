import pandas as pd

from stocks.indicators import atr
from stocks.models import Direction, SignalEvent


class ATRBreakoutStrategy:
    """通用型自適應策略：收盤價創過去N日新高（唐奇安通道上軌）就進場。出場預設用固定15%
    移動停損(stop_mode="pct")，也支援N倍ATR移動停損(stop_mode="atr"，波動大的股票停損
    空間自動放寬、波動小的自動收窄)——2026-08-15用scripts/backtest_stop_comparison.py
    全觀察清單10年回測比較過：這支策略原本只有單純ATR停損、沒有其他出場條件，改成固定
    15%後平均報酬/加總報酬/獲利因子全面提升(獲利因子2.36→3.45)，才改成15%當預設，
    ATR版本原本容易在反彈初期被正常波動洗出場，抓不到後面的大波段。停損只進不退：
    每天先用「前一天算出的停損線」判斷是否出場，沒出場才用當天收盤價把停損線往上拉，
    避免用當天收盤價同時決定當天的出場與停損位置（look-ahead）。"""

    name = "atr_breakout"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        donchian_period = params.get("donchian_period", 20)
        atr_period = params.get("atr_period", 14)
        atr_multiplier = params.get("atr_multiplier", 2)
        stop_mode = params.get("stop_mode", "pct")  # "atr"、"pct" 或 "tiered_pct"
        stop_pct = params.get("stop_pct", 0.15)
        stop_pct_half = params.get("stop_pct_half", 0.08)
        stop_pct_full = params.get("stop_pct_full", 0.15)

        close = bars["close"]
        donchian_upper = bars["high"].rolling(window=donchian_period).max().shift(1)

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
                elif c > donchian_upper[t]:
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
            elif c > donchian_upper[t]:
                stop = next_stop(c, t)
                events.append(
                    SignalEvent(symbol, self.name, Direction.BUY, c, t, f"創{donchian_period}日新高突破，{stop_label} {stop:.2f}")
                )
                in_position = True

        return events
