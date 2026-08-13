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
from stocks.strategies.long_swing import LongSwingStrategy
from stocks.strategies.ma_alignment import MAAlignmentStrategy
from stocks.strategies.ma_crossover import MACrossoverStrategy
from stocks.strategies.ma_trend import MATrendStrategy
from stocks.strategies.macd_strategy import MACDStrategy
from stocks.strategies.price_alert import PriceAlertStrategy
from stocks.strategies.rsi_strategy import RSIStrategy
from stocks.strategies.trend_following import TrendFollowingStrategy
from stocks.strategies.trust_momentum import TrustMomentumStrategy
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
    "trust_momentum": TrustMomentumStrategy(),
    "trend_following": TrendFollowingStrategy(),
    "breakout": BreakoutStrategy(),
    "golden_cross_scaleout": GoldenCrossScaleOutStrategy(),
    "long_swing": LongSwingStrategy(),
}

STRATEGY_LABELS: dict[str, str] = {
    "ma_crossover": "均線交叉 (5/20日)",
    "rsi": "RSI超買超賣",
    "macd": "MACD交叉",
    "bollinger": "布林通道",
    "volume_anomaly": "成交量異常",
    "price_alert": "到價提醒",
    "ma_alignment": "多空排列 (5/10/20日線)",
    "kd": "KD低檔黃金交叉/高檔死亡交叉",
    "institutional_streak": "三大法人連續買賣超",
    "ma_trend": "站上5/20日均線且20日線上揚",
    "atr_breakout": "ATR動態通道突破(創20日新高進場，2倍ATR移動停損出場)",
    "chip_momentum": "外資買超動能(連3日買超+未超買進場，2.5倍ATR移動停損出場)",
    "trust_momentum": "投信買超動能(近5日≥3天買超且淨額為正+未超買進場，2.5倍ATR移動停損出場)",
    "trend_following": "趨勢追蹤(20>60日均線+站上20日線+爆量進場，跌破20日線/均線反轉出場)",
    "breakout": "Breakout突破(創20日新高+爆量進場，跌破10日最低出場)",
    "golden_cross_scaleout": "均線黃金交叉分批出場(打分制進場≥5分，跌破5日線+量能先賣一半，跌破10日線或死亡交叉賣剩餘)",
    "long_swing": "中長波段(60>120日均線多頭+法人買超進場，站回20日線且60日線上揚可重新進場，跌破均線3天或3.5倍ATR停損出場)",
}
# 放這裡(不是dashboard/app.py)是因為notifier.py的Telegram通知也要用同一份中文名稱，
# 兩處各自維護一份很容易一邊改了忘了改另一邊——策略名稱本身算strategy的metadata，
# 跟STRATEGY_REGISTRY放一起最自然。


def strategy_label(name: str) -> str:
    """策略/指標的英文鍵值(例如"chip_momentum")只在程式內部跟資料庫用，畫面上/通知裡
    一律要顯示中文——STRATEGY_LABELS的中文說明常常後面還帶一段括號解釋參數，這裡只取
    名稱本體。"""
    return STRATEGY_LABELS.get(name, name).split("(")[0]
