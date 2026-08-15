import pandas as pd

from stocks.indicators import atr, macd, rsi, stochastic_kd
from stocks.models import Direction, SignalEvent


class BullishDivergenceStrategy:
    """價格創近N日新低，但RSI沒有跟著創新低(背離)，代表下跌動能已經在減弱——比單純
    「RSI很低」更早抓到轉折，抓的是「這次探底跌勢有沒有比上次更兇」，而不是「現在夠不夠
    便宜」。跟rsi_mean_reversion(短週期RSI(2)超賣+跌破布林下軌，設計給盤整行情用)不同，
    這支用長週期RSI(14)、目標是抓單邊崩跌後的真正止穩，避免強趨勢中被巴來巴去。
    出場預設用固定15%移動停損(stop_mode="pct")，也支援ATR移動停損(stop_mode="atr")跟
    分批停損(stop_mode="tiered_pct"：跌8%先賣一半、跌15%賣剩餘一半，2026-08-15新增
    供實測比較用)——
    2026-08-15用scripts/backtest_bottom_pickers.py實測比較8%/10%/15%固定停損跟2.5倍
    ATR停損：8%/10%太緊，在崩跌剛止穩、行情還在震盪的階段常常被正常雜訊洗出場，抓不到
    後面真正的大反轉；15%版本三個策略(這支+capitulation_reversal+chip_reversal_fast)
    的平均報酬、加總報酬、獲利因子全面勝出(獲利因子2.01→2.96)，才改成15%當預設。
    最大回撤數字看起來比ATR版更深，但那是單筆振幅變大的正常結果(賺賠都放大)，不代表
    策略更不穩定——同樣的判斷邏輯用在strategy_selection.py的排除規則上：獲利因子才是
    判斷好壞的依據，不是MDD。"""

    name = "bullish_divergence"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        lookback_days = params.get("lookback_days", 20)
        rsi_period = params.get("rsi_period", 14)
        rsi_ceiling = params.get("rsi_ceiling", 40)
        atr_period = params.get("atr_period", 14)
        atr_multiplier = params.get("atr_multiplier", 2.5)
        stop_mode = params.get("stop_mode", "pct")  # "atr"、"pct" 或 "tiered_pct"
        stop_pct = params.get("stop_pct", 0.15)
        stop_pct_half = params.get("stop_pct_half", 0.08)
        stop_pct_full = params.get("stop_pct_full", 0.15)
        confirm_next_day = params.get("confirm_next_day", False)  # 2026-08-15研究中：
        # 2026年7月的系統性重挫期間這支策略進場後平均還要再跌一段(13~28個交易日後才真正
        # 落底)，訊號當天訊號一出現就進場，等於接刀。跟capitulation_reversal同一套「隔天
        # 不再破前低+收盤收高才進場」的確認邏輯借過來試——那支策略同一段期間完全沒有誤觸發，
        # 差別就在這個確認機制。預設False不影響既有行為。
        require_macd_turn = params.get("require_macd_turn", False)  # 額外要求MACD柱狀圖
        # 比前一天回升(下跌動能減弱的另一個角度佐證，不要求真正黃金交叉——那個太罕見，
        # 幾乎篩不出任何訊號)。
        require_kd_bullish = params.get("require_kd_bullish", False)  # 額外要求K>D(偏多)。

        close = bars["close"]
        rsi_value = rsi(close, rsi_period)

        # shift(1)：不含當天，跟atr_breakout的唐奇安通道一樣避免look-ahead(當天的低點
        # 本身不該拿來當「跌破前N日低點」的判斷基準)。
        rolling_low_price = close.rolling(window=lookback_days).min().shift(1)
        rolling_low_rsi = rsi_value.rolling(window=lookback_days).min().shift(1)

        makes_new_low = close <= rolling_low_price
        rsi_diverges = rsi_value > rolling_low_rsi
        not_too_strong = rsi_value < rsi_ceiling  # 還在偏弱區間，避免已經強勢反彈完才進場

        entry_condition = makes_new_low & rsi_diverges & not_too_strong

        if require_macd_turn:
            _, _, histogram = macd(close)
            entry_condition = entry_condition & (histogram > histogram.shift(1))
        if require_kd_bullish:
            k, d = stochastic_kd(bars["high"], bars["low"], close)
            entry_condition = entry_condition & (k > d)

        prev_entry = entry_condition.shift(1).fillna(False).astype(bool)
        entry_edge = entry_condition & ~prev_entry

        if confirm_next_day:
            raw_signal = entry_edge.shift(1).fillna(False).astype(bool)
            entry_edge = raw_signal & (close > close.shift(1)) & (bars["low"] >= bars["low"].shift(1))

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
                elif entry_edge[t] and not pd.isna(rolling_low_rsi[t]):
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
                            f"價格創{lookback_days}日新低但RSI({rsi_period})未破底(背離)，"
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
            elif (
                entry_edge[t]
                and not pd.isna(rolling_low_rsi[t])
                and (stop_mode == "pct" or not pd.isna(atr_value[t]))
            ):
                stop = next_stop(c, t)
                events.append(
                    SignalEvent(
                        symbol,
                        self.name,
                        Direction.BUY,
                        c,
                        t,
                        f"價格創{lookback_days}日新低但RSI({rsi_period})未破底(背離)，{stop_label} {stop:.2f}",
                    )
                )
                in_position = True

        return events
