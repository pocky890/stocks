import pandas as pd

from stocks.models import SignalEvent, Tier
from stocks.strategies import STRATEGY_REGISTRY


def evaluate_all(
    symbol: str,
    bars: pd.DataFrame,
    strategy_params: dict,
    tier: Tier = Tier.REALTIME,
    price_alert_target: float | None = None,
    skip_strategies: set[str] | None = None,
) -> list[SignalEvent]:
    """Run every registered strategy against the full bars history given.
    Each strategy is edge-triggered internally; duplicate suppression across
    repeated/overlapping calls happens later at db.insert_signal_events().

    skip_strategies lets a caller opt specific strategies out entirely (e.g. the
    full-market batch scan skips institutional_streak, which only has data for the
    watchlist) without touching STRATEGY_REGISTRY itself."""
    events: list[SignalEvent] = []
    skip_strategies = skip_strategies or set()

    for name, strategy in STRATEGY_REGISTRY.items():
        if name in skip_strategies:
            continue
        params = dict(strategy_params.get(name, {}))
        if name == "price_alert":
            if price_alert_target is None:
                continue
            params["target_price"] = price_alert_target

        raw_events = strategy.evaluate(symbol, bars, params)
        events.extend(SignalEvent(**{**e.__dict__, "tier": tier}) for e in raw_events)

    return events
