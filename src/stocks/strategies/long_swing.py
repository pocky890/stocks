import pandas as pd

from stocks.indicators import atr, rsi, sma
from stocks.models import Direction, SignalEvent


class LongSwingStrategy:
    """中長波段策略：MA60>MA120站上半年線多頭排列(regime)為大方向，搭配法人籌碼、ATR移動
    停損、連續跌破均線出場——原始設計由使用者指定參數(trend_fast=60/trend_slow=120/
    atr_multiplier=3.5/chip_lookback_days=20/exit_confirm_days=3)。

    進場邏輯經過多輪全觀察清單回測驗證後調整，跟原始設計不同的地方：
    1. 「首次進場」跟「同一段regime裡的重新進場」分開處理——regime沒有斷過(MA60未跌破
       MA120)之前，被停損甩出去後只要站回20日均線就能重新進場，不用每次都重新等慢速的
       籌碼(20日)+RSI確認。原因：edge-triggered版本(進場條件只在剛觸發的那一刻算數)會在
       regime中途被停損出場後卡住，因為籌碼/RSI這類條件通常已經是True很久、不會重新
       「觸發」，導致行情噴出時完全進不了場、只能眼睜睜看著錯過整段(見8299 2024年的案例)。
    2. 寬鬆的「站回均線重新進場」額外要求MA60本身近5天仍在上升(斜率>0)，不是走平/下彎。
       原因：純用regime判斷(MA60>MA120)太遲鈍，行情走平/緩跌時regime名義上還沒結束，
       寬鬆重進場規則會反覆被小洗盤打到出場，造成3450連7次、8299連5次停損出場的問題。
       實測比較過「連續虧損斷路器」「進場後獲利提前鎖利收緊停損」「縮短籌碼觀察窗口+放寬
       RSI」都無效或反而拖累總報酬(獲利鎖利尤其明顯——會系統性砍掉這個策略真正的獲利
       來源：少數幾筆抱到滿(500%+)的巨大波段，這是趨勢策略的本質，不是缺陷)；只有MA60
       斜率濾網做到「總報酬沒有變差、勝率還提升」的改善，因此採用。斜率走平時仍退回
       要求完整條件(籌碼+RSI)才能重進，不是完全不能進場。
    3. 個別股票(如6442/3189)在特定期間即使斜率濾網也救不了、仍會出現連續停損，這是
       這套策略設計下的正常代價，不為了少數股票把邏輯改得更複雜——真的持續不適合的話，
       用既有的個股策略排除機制(disabled_strategies)處理即可。
    4. 籌碼條件從「外資或投信近20日買超任一為正」改成「外資+投信合計近20日買超為正」——
       原本用「任一」會有土洋對作的漏洞：一邊法人大買、另一邊大賣，個別任一為正就放行，
       但整體籌碼其實是渙散的。這是外部review(Gemini)提出的建議，經全觀察清單回測驗證
       後採用：3189連續停損5→1、6442連續停損5→4，加總報酬、勝率、中位數同時變好，
       是目前測過所有調整裡最全面的一次改善(2454這檔小幅犧牲了一點總報酬，其餘股票
       多數不受影響，整體是淨改善)。同一次review另外兩點(重進場均線位階、ATR停損
       重置)查證後發現本來就沒有問題(price_above_fast已經確保重進場一定在MA60之上;
       stop每次進場都是None重新算，不會延用上一趟的舊高點)；「RSI超買但爆量可豁免」
       這點回測後總報酬微升但3450連續停損從4惡化到5，效果又高度集中在單一股票(6442)
       一次早期卡位，不夠乾淨，沒有採用。
    5. RSI超買門檻從70放寬到75——回測驗證後採用：全觀察清單加總報酬+3.2%(2088.1→
       2155.2)，9檔完全不受影響，6442吃到更多早期進場(其中一筆+201.5%的大波段)，
       代價只有8299一筆-4.2%的小虧損(連續停損4→5，可接受)。跟上面「爆量豁免」比起來
       這個調整乾淨得多，多數股票不受影響，不是靠單一運氣撐起來的。

    沒有foreign_net/trust_net任一欄位(bars沒join到institutional_flows)就直接跳過，
    跟chip_momentum/trust_momentum一樣的防護。"""

    name = "long_swing"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        if "foreign_net" not in bars.columns and "trust_net" not in bars.columns:
            return []

        trend_fast = params.get("trend_fast", 60)
        trend_slow = params.get("trend_slow", 120)
        atr_period = params.get("atr_period", 20)
        atr_multiplier = params.get("atr_multiplier", 3.5)
        chip_lookback_days = params.get("chip_lookback_days", 20)
        exit_confirm_days = params.get("exit_confirm_days", 3)
        rsi_overbought = params.get("rsi_overbought", 75)
        reentry_ma_period = params.get("reentry_ma_period", 20)
        slope_lookback = params.get("slope_lookback", 5)
        stop_mode = params.get("stop_mode", "atr")  # "atr" 或 "pct"，2026-08-15新增供實測比較用——
        # 注意本策略docstring第20-24行已經記錄過「獲利提前鎖利收緊停損」實測後總報酬變差
        # (會系統性砍掉少數抱到滿500%+的巨大波段，這是這支策略的獲利來源，不是缺陷)，
        # 固定15%停損是否會踩到同樣的問題要看實測，不要預設一定更好。
        stop_pct = params.get("stop_pct", 0.15)

        close = bars["close"]
        ma_fast = sma(close, trend_fast)
        ma_slow = sma(close, trend_slow)
        ma_reentry = sma(close, reentry_ma_period)
        atr_value = atr(bars["high"], bars["low"], close, atr_period)

        foreign_net = bars.get("foreign_net", pd.Series(0, index=bars.index)).fillna(0)
        trust_net = bars.get("trust_net", pd.Series(0, index=bars.index)).fillna(0)
        chip_support = (foreign_net + trust_net).rolling(chip_lookback_days).sum() > 0
        not_overbought = rsi(close, 14) < rsi_overbought
        trend_strong = ma_fast.diff(slope_lookback) > 0

        regime_active = ma_fast > ma_slow
        price_above_fast = close > ma_fast
        price_above_reentry = close > ma_reentry

        below_fast = close < ma_fast
        group_id = (below_fast != below_fast.shift()).cumsum()
        below_streak = below_fast.groupby(group_id).cumcount() + 1
        exit_confirmed = below_fast & (below_streak >= exit_confirm_days)

        def next_stop(c: float, t) -> float:
            if stop_mode == "pct":
                return c * (1 - stop_pct)
            return c - atr_multiplier * atr_value[t]

        stop_label = f"{stop_pct * 100:.0f}%移動停損" if stop_mode == "pct" else f"{atr_multiplier}倍ATR停損"

        events: list[SignalEvent] = []
        in_position = False
        stop = None
        had_entry_this_regime = False

        for t in bars.index:
            c = close[t]
            if pd.isna(ma_slow[t]) or (stop_mode == "atr" and pd.isna(atr_value[t])) or pd.isna(trend_strong[t]):
                continue
            if not regime_active[t]:
                had_entry_this_regime = False

            if in_position:
                exit_stop = c < stop
                if exit_confirmed[t] or exit_stop:
                    reasons = []
                    if exit_confirmed[t]:
                        reasons.append(f"連續{exit_confirm_days}天跌破{trend_fast}日均線")
                    if exit_stop:
                        reasons.append(f"跌破{stop_label} {stop:.2f}")
                    events.append(SignalEvent(symbol, self.name, Direction.SELL, c, t, "、".join(reasons)))
                    in_position = False
                    stop = None
                else:
                    stop = max(stop, next_stop(c, t))
            elif regime_active[t] and price_above_fast[t]:
                if not had_entry_this_regime:
                    if chip_support[t] and not_overbought[t]:
                        stop = next_stop(c, t)
                        in_position = True
                        had_entry_this_regime = True
                        events.append(
                            SignalEvent(
                                symbol,
                                self.name,
                                Direction.BUY,
                                c,
                                t,
                                f"首次進場：{trend_fast}日>{trend_slow}日均線多頭排列，法人近{chip_lookback_days}日買超為正，RSI未超買",
                            )
                        )
                elif price_above_reentry[t] and trend_strong[t]:
                    stop = next_stop(c, t)
                    in_position = True
                    events.append(
                        SignalEvent(
                            symbol,
                            self.name,
                            Direction.BUY,
                            c,
                            t,
                            f"同趨勢重新進場：站回{reentry_ma_period}日均線且{trend_fast}日均線仍上揚",
                        )
                    )
                elif price_above_reentry[t] and chip_support[t] and not_overbought[t]:
                    stop = next_stop(c, t)
                    in_position = True
                    events.append(
                        SignalEvent(
                            symbol,
                            self.name,
                            Direction.BUY,
                            c,
                            t,
                            f"{trend_fast}日均線走平但法人+RSI條件通過重新進場",
                        )
                    )

        return events
