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


def summarize_trades(trades: list[Trade]) -> dict:
    """回傳None代表沒有任何一次完整的進出場，勝率/報酬無意義。"""
    if not trades:
        return None
    returns = [t.return_pct for t in trades]
    wins = sum(1 for r in returns if r > 0)
    return {
        "n": len(trades),
        "win_rate": wins / len(trades) * 100,
        "avg_return_pct": sum(returns) / len(returns),
    }
