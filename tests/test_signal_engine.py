import pandas as pd

from stocks.models import Direction, Tier
from stocks.signal_engine import evaluate_all


def make_bars(closes):
    index = pd.date_range("2026-01-02", periods=len(closes), freq="D")
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": [1000] * len(closes)},
        index=index,
    )


def test_evaluate_all_runs_every_registered_strategy_and_tags_tier():
    closes = [10, 10, 10, 10, 12, 14, 16, 18, 16, 12, 8, 4, 2]
    bars = make_bars(closes)
    events = evaluate_all("2330", bars, {"ma_crossover": {"fast": 2, "slow": 4}}, tier=Tier.BATCH)

    assert any(e.strategy == "ma_crossover" for e in events)
    assert all(e.tier == Tier.BATCH for e in events)


def test_evaluate_all_skips_price_alert_without_target():
    bars = make_bars([10, 20, 30])
    events = evaluate_all("2330", bars, {})
    assert all(e.strategy != "price_alert" for e in events)


def test_evaluate_all_includes_price_alert_when_target_given():
    closes = [90, 95, 105, 95]
    bars = make_bars(closes)
    events = evaluate_all("2330", bars, {}, price_alert_target=100)
    price_alert_events = [e for e in events if e.strategy == "price_alert"]
    assert len(price_alert_events) == 2
    assert {e.direction for e in price_alert_events} == {Direction.BUY, Direction.SELL}
