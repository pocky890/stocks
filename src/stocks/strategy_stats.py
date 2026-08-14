"""把一個策略自己的BUY/SELL訊號串成一次一次的進出場，算歷史勝率/報酬率——給dashboard
「這個策略在這支股票的歷史表現」參考用，不是自動下單依據。只對「進場+出場邏輯綁在一起」的
策略(NOTIFIABLE_STRATEGIES)有意義；單一指標訊號(RSI/MACD/KD交叉...)本身不是設計成配對的
進出場系統，硬套這套邏輯算出來的勝率只能當粗略參考，不是那些指標原本的用法。"""
from dataclasses import dataclass

from stocks.models import Direction, SignalEvent


@dataclass(frozen=True)
class Trade:
    entry_ts: object
    entry_price: float
    entry_detail: str
    exit_ts: object
    exit_price: float
    exit_detail: str

    @property
    def return_pct(self) -> float:
        return (self.exit_price - self.entry_price) / self.entry_price * 100


def simulate_round_trips(events: list[SignalEvent]) -> tuple[list[Trade], SignalEvent | None]:
    """events不需要事先排序。Flat時遇到BUY進場；進場後遇到下一個SELL才出場配成一筆——
    連續同方向的訊號(還沒賣就又來一個買)會被忽略，因為還沒有能力做加碼。"""
    trades = []
    in_position = False
    entry = None

    for e in sorted(events, key=lambda e: e.ts):
        if not in_position and e.direction == Direction.BUY:
            entry = e
            in_position = True
        elif in_position and e.direction == Direction.SELL:
            trades.append(
                Trade(
                    entry_ts=entry.ts,
                    entry_price=entry.price,
                    entry_detail=entry.detail,
                    exit_ts=e.ts,
                    exit_price=e.price,
                    exit_detail=e.detail,
                )
            )
            in_position = False
            entry = None

    open_position = entry if in_position else None
    return trades, open_position


def _max_drawdown_pct(trades: list) -> float:
    """把每筆交易的return_pct依進場時間(entry_ts，Trade跟ScaleoutTrade都有這個欄位，
    不像exit_ts只有Trade有)直接加總(不是複利，跟total_return_pct同一套慣例)畫出一條
    簡化權益曲線，抓這條線從高點到低點最大跌了多少——沒有真正的資金/倉位大小模型，
    是「如果每筆交易都投入同一單位」的簡化版，抓的是相對走勢，不是實際資金曲線。
    回傳正數(跌幅大小)，0代表這條曲線從頭到尾沒有回落過。"""
    ordered = sorted(trades, key=lambda t: t.entry_ts)
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in ordered:
        cumulative += t.return_pct
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)
    return max_dd


def summarize_trades(trades: list[Trade]) -> dict:
    """回傳None代表沒有任何一次完整的進出場，勝率/報酬無意義。

    avg_return_excluding_best_pct：拿掉單筆報酬率最高的那一筆之後剩下的平均——趨勢跟隨型
    策略(atr_breakout/chip_momentum/trust_momentum等)本來就是靠少數幾筆大波段撐報酬，
    低勝率不代表不好，但如果拿掉那"一筆"最好的之後剩下全部轉負，代表這個組合的正報酬
    只是運氣好抓到一次，不是可以重複期待的表現——用來給strategy_selection.py判斷排除
    時當「這個正報酬夠不夠紮實」的防呆檢查，不是要否定低勝率高賺賠比這種策略類型本身。
    只有1筆交易時沒有「剩下的」可以算，回傳None。

    2026-08-17使用者(轉述Gemini的建議)要求補上兩個原本完全沒追蹤的風險指標：
    - profit_factor(獲利因子) = 總獲利/總虧損(絕對值)，跟勝率是不同維度——勝率只算
      次數，獲利因子看的是「賺賠的大小比」，趨勢跟隨策略常見「勝率不到50%但賺賠比夠大」
      的樣貌，獲利因子能把這個特質量化出來。完全沒有虧損時(losses=0)比值沒有意義，
      回傳None，不是0或無限大。
    - max_drawdown_pct(最大回撤)：用一條簡化權益曲線(見_max_drawdown_pct)抓「中間
      最慘從高點跌了多少」——只看平均/加總報酬看不出這個策略過程中會不會讓人心理上
      拿不住(例如中途曾經跌50%，即使最後總報酬是正的，大部分人也撐不到那個時候)。"""
    if not trades:
        return None
    returns = [t.return_pct for t in trades]
    wins = sum(1 for r in returns if r > 0)
    sorted_returns = sorted(returns, reverse=True)
    remaining = sorted_returns[1:]
    gains = sum(r for r in returns if r > 0)
    losses = abs(sum(r for r in returns if r < 0))
    return {
        "n": len(trades),
        "win_rate": wins / len(trades) * 100,
        "avg_return_pct": sum(returns) / len(returns),
        "total_return_pct": sum(returns),
        "avg_return_excluding_best_pct": (sum(remaining) / len(remaining)) if remaining else None,
        "profit_factor": (gains / losses) if losses > 0 else None,
        "max_drawdown_pct": _max_drawdown_pct(trades),
    }


@dataclass(frozen=True)
class ScaleoutTrade:
    """golden_cross_scaleout一次進場配兩次出場(先賣一半、再賣剩餘一半)，跟Trade
    「一買配一賣」的形狀不一樣，報酬率用兩次出場價的均價(各半)計算。return_pct跟
    Trade同名同義，所以summarize_trades()可以直接吃ScaleoutTrade的list，不用另外
    寫一份summarize邏輯。"""

    entry_ts: object
    entry_price: float
    exit1_ts: object
    exit1_price: float
    exit2_ts: object
    exit2_price: float

    @property
    def blended_exit_price(self) -> float:
        return (self.exit1_price + self.exit2_price) / 2

    @property
    def return_pct(self) -> float:
        return (self.blended_exit_price - self.entry_price) / self.entry_price * 100


def simulate_scaleout_trades(events: list[SignalEvent]) -> tuple[list[ScaleoutTrade], dict | None]:
    """跟simulate_round_trips配對邏輯不一樣：一次BUY要收集到兩次SELL才算平倉(對應
    golden_cross_scaleout「賣一半、再賣剩餘一半」的兩階段出場)，直接套simulate_round_trips
    會把第一次半倉出場當成整筆平倉、丟掉第二次出場價格，報酬率會算錯。回傳
    (trades, still_open)，still_open是資料結束時還沒配滿兩次出場的部位(可能兩次都
    沒賣、或只賣了一半)：{"entry": SignalEvent, "exits": list[SignalEvent]}。"""
    trades = []
    entry = None
    exits: list[SignalEvent] = []
    for e in sorted(events, key=lambda e: e.ts):
        if e.direction == Direction.BUY and entry is None:
            entry, exits = e, []
        elif e.direction == Direction.SELL and entry is not None:
            exits.append(e)
            if len(exits) == 2:
                trades.append(
                    ScaleoutTrade(
                        entry_ts=entry.ts,
                        entry_price=entry.price,
                        exit1_ts=exits[0].ts,
                        exit1_price=exits[0].price,
                        exit2_ts=exits[1].ts,
                        exit2_price=exits[1].price,
                    )
                )
                entry, exits = None, []

    still_open = {"entry": entry, "exits": exits} if entry is not None else None
    return trades, still_open
