import pandas as pd

from stocks.indicators import rolling_avg_volume, rsi, sma
from stocks.models import Direction, SignalEvent


class GoldenCrossScaleOutStrategy:
    """進場打分制(2026-08-07第三次調整版，加上RSI濾網)：
      MA5 > MA20            +2
      收盤站上MA20           +1
      三大法人(外資+投信)近5日合計買超  +2
      收盤突破20日新高(不含當天，跟atr_breakout同樣shift(1)避免look-ahead) +2
      當天成交量 > 20日均量   +1
      RSI還沒超買(<rsi_overbought，預設70)  +1
      總分達到score_threshold(預設5分)才進場，edge-triggered(總分從<5變成>=5的第一天觸發，
      不是每天符合就重複發)。前5項是「動能夠不夠強」，RSI濾網問的是不同維度的問題「是不是
      已經追太高了」——回測2454發現有些訊號分數很高但立刻反轉，一部分是追在超買區進場，
      RSI在那種情況不加分，score就少1分。「法人5日買超」用「近5日合計>0」(不是連續買超
      天數)，跟其他項一樣是加分項，不是硬性關卡，所以缺一項(例如籌碼沒過)只要其他項夠強
      還是能靠分數補上。

    出場(2026-08-07定案版，用回測比較過6種進場/出場組合後確認這版勝率/平均報酬最高，
    比賣出打分制(嘗試過6項加權)、2日確認+20日均線全出(嘗試過)兩版實測都好，回退到這版
    固定為現行版本)，分兩階段、不是一次全出：
      階段1(賣一半)：收盤跌破5日均線，且當天成交量 > 20日均量(量能確認的真跌破，不是量縮
        小回檔)
      階段2(賣剩餘一半)：收盤跌破10日均線，或5日均線跌破20日均線(死亡交叉)，兩者任一即可

    用兩個獨立的SELL事件代表兩次出場動作，detail會標明「賣出一半」/「賣出剩餘一半」——
    跟其他策略「一次全出」的形狀不一樣，backtest_formula.py要用專門的配對邏輯
    (simulate_scaleout_trades)才能正確算報酬率，不能直接套strategy_stats的一買配一賣。"""

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
        prev_entry_state = entry_state.shift(1).fillna(False).astype(bool)
        entry_edge = entry_state & ~prev_entry_state

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
            if position == 0 and entry_edge[t]:
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
