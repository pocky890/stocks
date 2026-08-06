from datetime import datetime

from stocks.models import Direction, SignalEvent
from stocks.strategy_stats import simulate_round_trips, summarize_trades


def make_event(direction, price, ts):
    return SignalEvent(symbol="2330", strategy="chip_momentum", direction=direction, price=price, ts=ts)


def test_simulate_round_trips_pairs_buy_with_next_sell():
    events = [
        make_event(Direction.BUY, 100.0, datetime(2026, 1, 1)),
        make_event(Direction.SELL, 110.0, datetime(2026, 1, 5)),
        make_event(Direction.BUY, 90.0, datetime(2026, 1, 10)),
        make_event(Direction.SELL, 81.0, datetime(2026, 1, 15)),
    ]

    trades, open_position = simulate_round_trips(events)

    assert len(trades) == 2
    assert trades[0].return_pct == 10.0
    assert trades[1].return_pct == -10.0
    assert open_position is None


def test_simulate_round_trips_ignores_repeated_buy_while_already_in_position():
    events = [
        make_event(Direction.BUY, 100.0, datetime(2026, 1, 1)),
        make_event(Direction.BUY, 105.0, datetime(2026, 1, 3)),  # 還沒賣，忽略這個重複買訊號
        make_event(Direction.SELL, 120.0, datetime(2026, 1, 5)),
    ]

    trades, open_position = simulate_round_trips(events)

    assert len(trades) == 1
    assert trades[0].entry_price == 100.0  # 用第一筆買進價，不是被忽略的那筆
    assert open_position is None


def test_simulate_round_trips_reports_open_position_when_never_exited():
    events = [make_event(Direction.BUY, 100.0, datetime(2026, 1, 1))]

    trades, open_position = simulate_round_trips(events)

    assert trades == []
    assert open_position.price == 100.0


def test_summarize_trades_computes_win_rate_and_avg_return():
    events = [
        make_event(Direction.BUY, 100.0, datetime(2026, 1, 1)),
        make_event(Direction.SELL, 110.0, datetime(2026, 1, 5)),
        make_event(Direction.BUY, 90.0, datetime(2026, 1, 10)),
        make_event(Direction.SELL, 81.0, datetime(2026, 1, 15)),
    ]
    trades, _ = simulate_round_trips(events)

    summary = summarize_trades(trades)

    assert summary["n"] == 2
    assert summary["win_rate"] == 50.0
    assert summary["avg_return_pct"] == 0.0


def test_summarize_trades_returns_none_when_no_closed_trades():
    assert summarize_trades([]) is None
