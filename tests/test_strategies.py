import pandas as pd

from stocks.models import Direction
from stocks.strategies.bollinger import BollingerStrategy
from stocks.strategies.ma_alignment import MAAlignmentStrategy
from stocks.strategies.ma_crossover import MACrossoverStrategy
from stocks.strategies.macd_strategy import MACDStrategy
from stocks.strategies.price_alert import PriceAlertStrategy
from stocks.strategies.rsi_strategy import RSIStrategy
from stocks.strategies.volume_anomaly import VolumeAnomalyStrategy


def make_bars(closes, volumes=None, opens=None, highs=None, lows=None):
    n = len(closes)
    index = pd.date_range("2026-01-02", periods=n, freq="D")
    return pd.DataFrame(
        {
            "open": opens or closes,
            "high": highs or closes,
            "low": lows or closes,
            "close": closes,
            "volume": volumes or [1000] * n,
        },
        index=index,
    )


def test_ma_crossover_fires_once_per_direction():
    closes = [10, 10, 10, 10, 12, 14, 16, 18, 16, 12, 8, 4, 2]
    bars = make_bars(closes)
    events = MACrossoverStrategy().evaluate("2330", bars, {"fast": 2, "slow": 4})

    buys = [e for e in events if e.direction == Direction.BUY]
    sells = [e for e in events if e.direction == Direction.SELL]
    assert len(buys) == 1, "flat-to-uptrend should fire exactly one golden cross"
    assert len(sells) == 1, "uptrend-to-downtrend should fire exactly one death cross"
    assert buys[0].ts < sells[0].ts


def test_rsi_fires_buy_then_sell_on_trend_reversal():
    # oscillates near neutral first (RSI ~33-66, not already extreme), then a decline pushes
    # RSI below 30 once, then a climb pushes it above 70 once.
    closes = [50, 51, 50, 51, 50, 51, 50, 49, 47, 44, 40, 35, 40, 48, 58, 70, 82]
    bars = make_bars(closes)
    events = RSIStrategy().evaluate("2330", bars, {"period": 3, "oversold": 30, "overbought": 70})

    buys = [e for e in events if e.direction == Direction.BUY]
    sells = [e for e in events if e.direction == Direction.SELL]
    assert len(buys) == 1, "RSI should cross below 30 exactly once, not re-fire while it stays low"
    assert len(sells) == 1, "RSI should cross above 70 exactly once"
    assert buys[0].ts < sells[0].ts


def test_macd_fires_bullish_then_bearish_on_trend_reversal():
    uptrend = list(range(1, 20))
    downtrend = list(range(20, 1, -1))
    bars = make_bars(uptrend + downtrend)
    events = MACDStrategy().evaluate("2330", bars, {"fast": 3, "slow": 6, "signal": 3})

    buys = [e for e in events if e.direction == Direction.BUY]
    sells = [e for e in events if e.direction == Direction.SELL]
    assert len(buys) >= 1
    assert len(sells) >= 1
    assert buys[0].ts < sells[-1].ts


def test_bollinger_fires_on_spike_above_and_plunge_below():
    # a rolling window always partly absorbs its own outlier into mean/std, so a spike needs a
    # long, stable baseline behind it to still clear the band it helped compute -- and the edge
    # check needs the *previous* bar's band to already be valid (past the period-20 warmup).
    closes = [10] * 25 + [100] + [10] * 5 + [-80] + [10] * 3
    bars = make_bars(closes)
    events = BollingerStrategy().evaluate("2330", bars, {"period": 20, "num_std": 2})

    sells = [e for e in events if e.direction == Direction.SELL]
    buys = [e for e in events if e.direction == Direction.BUY]
    assert len(sells) >= 1, "spike above upper band should fire a sell (mean-reversion) signal"
    assert len(buys) >= 1, "plunge below lower band should fire a buy signal"


def test_volume_anomaly_direction_follows_price_move():
    volumes = [100] * 6 + [500] + [100] * 6 + [500]
    closes = [50, 50, 50, 50, 50, 50, 60] + [60, 60, 60, 60, 60, 60, 50]
    bars = make_bars(closes, volumes=volumes)
    events = VolumeAnomalyStrategy().evaluate("2330", bars, {"avg_period": 5, "multiplier": 2})

    buys = [e for e in events if e.direction == Direction.BUY]
    sells = [e for e in events if e.direction == Direction.SELL]
    assert len(buys) == 1, "volume spike on an up day should be a buy signal"
    assert len(sells) == 1, "volume spike on a down day should be a sell signal"


def test_price_alert_fires_on_cross_up_and_cross_down():
    closes = [90, 95, 99, 105, 110, 105, 99, 95, 90]
    bars = make_bars(closes)
    events = PriceAlertStrategy().evaluate("2330", bars, {"target_price": 100})

    buys = [e for e in events if e.direction == Direction.BUY]
    sells = [e for e in events if e.direction == Direction.SELL]
    assert len(buys) == 1, "crossing above the target price fires exactly one buy alert"
    assert len(sells) == 1, "crossing back below fires exactly one sell alert"


def test_price_alert_returns_nothing_without_target():
    bars = make_bars([90, 95, 105])
    assert PriceAlertStrategy().evaluate("2330", bars, {}) == []


def test_ma_alignment_buy_once_and_sell_per_broken_ma():
    # below all MAs, rises above all three (single buy), then falls, breaking each MA in turn
    closes = [5, 5, 5, 5, 5] + [10, 12, 14, 16] + [8, 6, 4, 2]
    bars = make_bars(closes)
    events = MAAlignmentStrategy().evaluate("2330", bars, {"fast": 2, "mid": 3, "slow": 4})

    buys = [e for e in events if e.direction == Direction.BUY]
    sells = [e for e in events if e.direction == Direction.SELL]
    assert len(buys) == 1, "the AND-condition of being above all 3 MAs should be a single edge event"
    assert len(sells) == 3, "each of the 3 MAs being broken should fire its own independent sell event"
    assert {e.detail for e in sells} == {"跌破2日線", "跌破3日線", "跌破4日線"}
