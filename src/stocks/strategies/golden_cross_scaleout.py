import pandas as pd

from stocks.indicators import rolling_avg_volume, rsi, sma
from stocks.models import Direction, SignalEvent


class GoldenCrossScaleOutStrategy:
    """均線黃金交叉打分制策略。

    進場：以下6項各自加分，總分達到5分(score_threshold)才進場，level-triggered(當天
    分數達標就進場，不要求前一天未達標)：
      MA5 > MA20                 +2
      收盤站上MA20                +1
      三大法人(外資+投信)近5日合計買超    +2
      收盤突破20日新高(不含當天)        +2
      當天成交量 > 20日均量            +1
      RSI(14)未超買(<70)             +1

    出場：跌破15%移動停損(stop_mode="pct")，一買配一賣。

    也支援stop_mode="ma_scaleout"：分兩階段出場，不是一次全出：
      階段1(賣一半)：收盤跌破5日均線，且當天成交量>20日均量
      階段2(賣剩餘一半)：收盤跌破10日均線，或5日均線跌破20日均線(死亡交叉)
    這個模式一買配兩賣，要用simulate_scaleout_trades配對，不能套simulate_round_trips。

    斷路器：適用——全市場同產業≥60%股票跌破月線(20日均線)、且這支股票自己當下也跌破
    月線時，暫停新的BUY(SELL不受影響)。"""

    name = "golden_cross_scaleout"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        fast = params.get("fast", 5)
        mid = params.get("mid", 10)
        slow = params.get("slow", 20)
        chip_lookback_days = params.get("chip_lookback_days", 5)
        high_lookback_days = params.get("high_lookback_days", 20)
        volume_avg_period = params.get("volume_avg_period", 20)
        score_ma_cross = params.get("score_ma_cross", 2)
        score_above_slow = params.get("score_above_slow", 1)
        score_chip = params.get("score_chip", 2)
        score_breakout = params.get("score_breakout", 2)
        score_volume = params.get("score_volume", 1)
        score_rsi = params.get("score_rsi", 1)
        rsi_period = params.get("rsi_period", 14)
        rsi_overbought = params.get("rsi_overbought", 70)
        score_threshold = params.get("score_threshold", 5)
        stop_mode = params.get("stop_mode", "pct")  # "pct"(現行:單一15%移動停損全出，一買
        # 配一賣) 或 "ma_scaleout"(均線分批出場，一買配兩賣)
        stop_pct = params.get("stop_pct", 0.15)

        close = bars["close"]
        ma_fast = sma(close, fast)
        ma_mid = sma(close, mid)
        ma_slow = sma(close, slow)
        avg_vol = rolling_avg_volume(bars["volume"], volume_avg_period)
        volume_confirm = bars["volume"] > avg_vol

        ma_cross_up = ma_fast > ma_slow
        above_slow = close > ma_slow
        donchian_high = bars["high"].rolling(window=high_lookback_days).max().shift(1)
        breakout_high = close > donchian_high
        not_overbought = rsi(close, rsi_period) < rsi_overbought

        if "foreign_net" in bars.columns and "trust_net" in bars.columns:
            net_flow = (bars["foreign_net"].fillna(0) + bars["trust_net"].fillna(0)).rolling(window=chip_lookback_days).sum()
            chip_backed = net_flow > 0
        else:
            chip_backed = pd.Series(False, index=bars.index)

        score = (
            ma_cross_up.astype(int) * score_ma_cross
            + above_slow.astype(int) * score_above_slow
            + chip_backed.astype(int) * score_chip
            + breakout_high.astype(int) * score_breakout
            + volume_confirm.astype(int) * score_volume
            + not_overbought.astype(int) * score_rsi
        )
        entry_state = score >= score_threshold

        if stop_mode == "pct":
            events: list[SignalEvent] = []
            in_position = False
            stop = None

            for t in bars.index:
                c = close[t]
                if in_position:
                    if c < stop:
                        events.append(
                            SignalEvent(symbol, self.name, Direction.SELL, c, t, f"跌破{stop_pct * 100:.0f}%移動停損 {stop:.2f}")
                        )
                        in_position = False
                        stop = None
                    else:
                        stop = max(stop, c * (1 - stop_pct))
                elif entry_state[t]:
                    stop = c * (1 - stop_pct)
                    hits = [
                        label
                        for label, series in [
                            (f"MA{fast}>MA{slow}", ma_cross_up),
                            (f"站上MA{slow}", above_slow),
                            (f"法人近{chip_lookback_days}日買超", chip_backed),
                            (f"突破{high_lookback_days}日新高", breakout_high),
                            ("量增", volume_confirm),
                            (f"RSI未超買(<{rsi_overbought})", not_overbought),
                        ]
                        if series[t]
                    ]
                    events.append(
                        SignalEvent(
                            symbol,
                            self.name,
                            Direction.BUY,
                            c,
                            t,
                            f"打分{score[t]}分達標({'、'.join(hits)})，{stop_pct * 100:.0f}%移動停損 {stop:.2f}",
                        )
                    )
                    in_position = True

            return events

        below_fast_confirmed = (close < ma_fast) & volume_confirm
        prev_below_fast_confirmed = below_fast_confirmed.shift(1).fillna(False).astype(bool)
        break_half_edge = below_fast_confirmed & ~prev_below_fast_confirmed

        exit_remaining_condition = (close < ma_mid) | (ma_fast < ma_slow)
        prev_exit_remaining = exit_remaining_condition.shift(1).fillna(False).astype(bool)
        break_remaining_edge = exit_remaining_condition & ~prev_exit_remaining

        events: list[SignalEvent] = []
        position = 0  # 0=空手, 2=全倉, 1=剩一半

        for t in bars.index:
            c = close[t]
            if position == 2 and break_half_edge[t]:
                events.append(
                    SignalEvent(symbol, self.name, Direction.SELL, c, t, f"跌破{fast}日均線且量能放大，賣出一半")
                )
                position = 1
            if position == 1 and break_remaining_edge[t]:
                reasons = []
                if close[t] < ma_mid[t]:
                    reasons.append(f"跌破{mid}日均線")
                if ma_fast[t] < ma_slow[t]:
                    reasons.append(f"{fast}日均線跌破{slow}日均線")
                events.append(
                    SignalEvent(symbol, self.name, Direction.SELL, c, t, "、".join(reasons) + "，賣出剩餘一半")
                )
                position = 0
            if position == 0 and entry_state[t]:
                hits = [
                    label
                    for label, series in [
                        (f"MA{fast}>MA{slow}", ma_cross_up),
                        (f"站上MA{slow}", above_slow),
                        (f"法人近{chip_lookback_days}日買超", chip_backed),
                        (f"突破{high_lookback_days}日新高", breakout_high),
                        ("量增", volume_confirm),
                        (f"RSI未超買(<{rsi_overbought})", not_overbought),
                    ]
                    if series[t]
                ]
                events.append(
                    SignalEvent(symbol, self.name, Direction.BUY, c, t, f"打分{score[t]}分達標({'、'.join(hits)})")
                )
                position = 2

        return events
