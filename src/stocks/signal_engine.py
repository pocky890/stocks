import pandas as pd

from stocks.models import SignalEvent, Tier
from stocks.strategies import STRATEGY_REGISTRY


def evaluate_all(
    symbol: str,
    bars: pd.DataFrame,
    strategy_params: dict,
    tier: Tier = Tier.REALTIME,
    price_alert_target: float | None = None,
) -> list[SignalEvent]:
    """Run every registered strategy against the full bars history given.
    Each strategy is edge-triggered internally; duplicate suppression across
    repeated/overlapping calls happens later at db.insert_signal_events()."""
    events: list[SignalEvent] = []

    for name, strategy in STRATEGY_REGISTRY.items():
        params = dict(strategy_params.get(name, {}))
        if name == "price_alert":
            if price_alert_target is None:
                continue
            params["target_price"] = price_alert_target

        raw_events = strategy.evaluate(symbol, bars, params)
        events.extend(SignalEvent(**{**e.__dict__, "tier": tier}) for e in raw_events)

    return events
