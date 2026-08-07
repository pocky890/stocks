from typing import Protocol

import pandas as pd

from stocks.models import SignalEvent


class Strategy(Protocol):
    name: str

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]: ...


from stocks.strategies.atr_breakout import ATRBreakoutStrategy
from stocks.strategies.bollinger import BollingerStrategy
from stocks.strategies.breakout import BreakoutStrategy
from stocks.strategies.chip_momentum import ChipMomentumStrategy
from stocks.strategies.golden_cross_scaleout import GoldenCrossScaleOutStrategy
from stocks.strategies.institutional_streak import InstitutionalStreakStrategy
from stocks.strategies.kd_strategy import KDStrategy
from stocks.strategies.ma_alignment import MAAlignmentStrategy
from stocks.strategies.ma_crossover import MACrossoverStrategy
from stocks.strategies.ma_trend import MATrendStrategy
from stocks.strategies.macd_strategy import MACDStrategy
from stocks.strategies.price_alert import PriceAlertStrategy
from stocks.strategies.rsi_strategy import RSIStrategy
from stocks.strategies.trend_following import TrendFollowingStrategy
from stocks.strategies.volume_anomaly import VolumeAnomalyStrategy

STRATEGY_REGISTRY: dict[str, Strategy] = {
    "ma_crossover": MACrossoverStrategy(),
    "rsi": RSIStrategy(),
    "macd": MACDStrategy(),
    "bollinger": BollingerStrategy(),
    "volume_anomaly": VolumeAnomalyStrategy(),
    "price_alert": PriceAlertStrategy(),
    "ma_alignment": MAAlignmentStrategy(),
    "kd": KDStrategy(),
    "institutional_streak": InstitutionalStreakStrategy(),
    "ma_trend": MATrendStrategy(),
    "atr_breakout": ATRBreakoutStrategy(),
    "chip_momentum": ChipMomentumStrategy(),
    "trend_following": TrendFollowingStrategy(),
    "breakout": BreakoutStrategy(),
    "golden_cross_scaleout": GoldenCrossScaleOutStrategy(),
}
