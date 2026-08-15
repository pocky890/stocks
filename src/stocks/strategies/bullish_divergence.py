import pandas as pd

from stocks.indicators import atr, macd, rsi, stochastic_kd
from stocks.models import Direction, SignalEvent


class BullishDivergenceStrategy:
    """價格創近N日新低，但RSI沒有跟著創新低(背離)，代表下跌動能已經在減弱——比單純
    「RSI很低」更早抓到轉折，抓的是「這次探底跌勢有沒有比上次更兇」，而不是「現在夠不夠
    便宜」。跟rsi_mean_reversion(短週期RSI(2)超賣+跌破布林下軌，設計給盤整行情用)不同，
    這支用長週期RSI(14)、目標是抓單邊崩跌後的真正止穩，避免強趨勢中被巴來巴去。
    出場預設用固定15%移動停損(stop_mode="pct")，也支援ATR移動停損(stop_mode="atr")、
    分批停損(stop_mode="tiered_pct"：跌8%先賣一半、跌15%賣剩餘一半，2026-08-15新增
    供實測比較用)，跟結構停損(stop_mode="structural"：固定在進場K棒低點再往下2%緩衝，
    進場後不再往上移動，2026-08-15使用者建議新增——抄底的假設就是「這裡是底」，連進場
    那天的最低點都跌破代表假設本身錯了，該立刻認賠，不該用固定15%這種跟這次進場邏輯
    無關的空間繼續扛)——
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
        stop_mode = params.get("stop_mode", "pct")  # "atr"、"pct"、"tiered_pct" 或 "structural"
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
        require_reversal_confirm = params.get("require_reversal_confirm", False)  # 2026-08-15
        # 使用者建議：創新低+RSI背離只代表「下跌動能減弱」，不代表當天就是底，訊號當天直接
        # 進場等於猜底部；改成隔天等到價格反轉訊號實際出現才進場，底部支撐是走出來的，不是
        # 猜出來的。跟confirm_next_day(要求隔天不再破前低+收盤收高)是同一類「等確認」的
        # 想法，但這裡的確認條件更嚴格、更具體。
        reversal_confirm_ma_period = params.get("reversal_confirm_ma_period", 5)  # 「站上均線」
        # 用哪一條均線——5日太敏感，一般助跌反彈就能站上，2026-08-15回測比較過改用10日。
        require_reversal_kd = params.get("require_reversal_kd", False)  # 確認訊號額外納入
        # KD(K>D)：2026-08-15使用者發現只看「站上均線/前高」門檻太低，一天雜訊反彈就過關，
        # 建議多納入KD/MACD再判斷。
        require_reversal_macd = params.get("require_reversal_macd", False)  # 確認訊號額外
        # 納入MACD柱狀圖回升，跟require_reversal_kd同一次建議。
        reversal_confirm_min_signals = params.get("reversal_confirm_min_signals", 1)  # 「價格
        # 站上均線/前高」、「KD偏多」、「MACD回升」三個確認訊號(只計入有納入的)裡至少要有
        # 幾個同時成立才算數——預設1(任一成立即可，門檻最低，也是原本只有價格確認時的
        # 行為)；使用者可以調高到2或3(全部都要)換取更嚴格的確認，代價是進場筆數更少。
        reversal_confirm_max_wait_days = params.get("reversal_confirm_max_wait_days", 10)  # 2026-08-15
        # 使用者發現原本「只看隔天一次」漏掉真正的反轉：實測案例(3105穩懋7/29訊號)裡
        # MACD/KD要到6個交易日後才真的轉正，隔天沒確認就永遠放棄=連底部都篩掉了。改成
        # 持續等到確認出現才進場，但不能無限期等——等太久代表這次低點的參考意義已經過時
        # (股價可能已經反彈一大段、風險報酬比不再有利)，預設最多等10個交易日，超過就放棄
        # 這次訊號、等下一次創新低+背離重新判斷。
        structural_stop_buffer_pct = params.get("structural_stop_buffer_pct", 0.02)  # 2026-08-15
        # 使用者建議：抄底的假設就是「這裡是底」，如果進場那根K線的最低點都跌破，代表這個
        # 假設本身就錯了，不該用跟這次進場邏輯無關的固定15%繼續扛——停損改成進場那天的
        # 最低點再往下抓一個緩衝(預設2%，抓一點雜訊空間避免正常影線就被洗出場)，出場空間
        # 通常比15%窄很多，符合「左側交易試錯成本要小」的精神。這個停損是固定的(進場後
        # 不會再往上移動)，因為它保護的是「進場當時的結構是否還成立」，不是拿來鎖定後續
        # 獲利用的移動停損。

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

        if require_reversal_confirm:
            confirm_ma = close.rolling(reversal_confirm_ma_period).mean()
            price_confirms = (close > confirm_ma) | (close > bars["high"].shift(1))
            confirm_signals = [price_confirms]
            if require_reversal_kd:
                confirm_k, confirm_d = stochastic_kd(bars["high"], bars["low"], close)
                confirm_signals.append(confirm_k > confirm_d)
            if require_reversal_macd:
                _, _, confirm_histogram = macd(close)
                confirm_signals.append(confirm_histogram > confirm_histogram.shift(1))
            signal_count = sum(s.fillna(False).astype(int) for s in confirm_signals)
            confirmed_ok = (signal_count >= reversal_confirm_min_signals).to_numpy()

            # 逐日掃描：創新低+背離當天只記下「等待確認」的狀態，不直接進場；接下來每天
            # 檢查確認條件有沒有出現，出現才算entry_edge，超過reversal_confirm_max_wait_days
            # 天還沒確認就放棄這次訊號。如果等待期間又出現新的創新低+背離(更低的低點)，
            # 用新的訊號日重新起算等待——新低點才是現在真正的參考基準。
            raw_signal = entry_edge.to_numpy()
            confirmed_edge = [False] * len(raw_signal)
            pending_idx = None
            for i in range(len(raw_signal)):
                if raw_signal[i]:
                    pending_idx = i
                    continue
                if pending_idx is None:
                    continue
                if i - pending_idx > reversal_confirm_max_wait_days:
                    pending_idx = None
                elif confirmed_ok[i]:
                    confirmed_edge[i] = True
                    pending_idx = None
            entry_edge = pd.Series(confirmed_edge, index=bars.index)

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
            if stop_mode == "structural":
                return bars["low"][t] * (1 - structural_stop_buffer_pct)
            return c - atr_multiplier * atr_value[t]

        if stop_mode == "pct":
            stop_label = f"{stop_pct * 100:.0f}%移動停損"
        elif stop_mode == "structural":
            stop_label = f"結構停損(進場K棒低點-{structural_stop_buffer_pct * 100:.0f}%)"
        else:
            stop_label = "ATR移動停損"

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
                elif stop_mode == "pct" or (stop_mode == "atr" and not pd.isna(atr_value[t])):
                    # structural停損進場後固定不動(保護的是進場當下的結構是否還成立，不是
                    # 拿來鎖定後續獲利用的移動停損)，所以這裡刻意不幫structural往上移動。
                    stop = max(stop, next_stop(c, t))
            elif (
                entry_edge[t]
                and not pd.isna(rolling_low_rsi[t])
                and (stop_mode in ("pct", "structural") or not pd.isna(atr_value[t]))
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
