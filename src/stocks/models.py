from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Direction(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Tier(str, Enum):
    REALTIME = "realtime"
    BATCH = "batch"


@dataclass(frozen=True)
class Bar:
    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class SignalEvent:
    symbol: str
    strategy: str
    direction: Direction
    price: float
    ts: datetime
    detail: str = ""
    tier: Tier = Tier.REALTIME
