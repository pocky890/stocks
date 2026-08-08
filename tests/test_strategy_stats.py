from datetime import datetime

import pytest

from stocks.models import Direction, SignalEvent
from stocks.strategy_stats import simulate_round_trips, simulate_scaleout_trades, summarize_trades


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
    assert summary["total_return_pct"] == 0.0


def test_summarize_trades_returns_none_when_no_closed_trades():
    assert summarize_trades([]) is None


def test_summarize_trades_excludes_only_the_single_best_trade():
    events = [
        make_event(Direction.BUY, 100.0, datetime(2026, 1, 1)),
        make_event(Direction.SELL, 90.0, datetime(2026, 1, 5)),  # -10%
        make_event(Direction.BUY, 100.0, datetime(2026, 1, 10)),
        make_event(Direction.SELL, 300.0, datetime(2026, 1, 15)),  # +200%，這筆是最大贏家
        make_event(Direction.BUY, 100.0, datetime(2026, 1, 20)),
        make_event(Direction.SELL, 95.0, datetime(2026, 1, 25)),  # -5%
    ]
    trades, _ = simulate_round_trips(events)

    summary = summarize_trades(trades)

    assert summary["avg_return_pct"] == pytest.approx((200 - 10 - 5) / 3)
    # 拿掉+200%那一筆，只剩-10%跟-5%兩筆虧損，平均應該是負的
    assert summary["avg_return_excluding_best_pct"] == pytest.approx(-7.5)


def test_summarize_trades_avg_return_excluding_best_is_none_with_a_single_trade():
    events = [make_event(Direction.BUY, 100.0, datetime(2026, 1, 1)), make_event(Direction.SELL, 110.0, datetime(2026, 1, 5))]
    trades, _ = simulate_round_trips(events)

    summary = summarize_trades(trades)

    assert summary["n"] == 1
    assert summary["avg_return_excluding_best_pct"] is None


def test_simulate_scaleout_trades_pairs_one_buy_with_two_sells():
    events = [
        make_event(Direction.BUY, 100.0, datetime(2026, 1, 1)),
        make_event(Direction.SELL, 110.0, datetime(2026, 1, 5)),  # 賣出一半
        make_event(Direction.SELL, 90.0, datetime(2026, 1, 8)),  # 賣出剩餘一半
    ]

    trades, still_open = simulate_scaleout_trades(events)

    assert len(trades) == 1
    assert trades[0].blended_exit_price == 100.0, "兩次出場價各半的均價：(110+90)/2"
    assert trades[0].return_pct == 0.0
    assert still_open is None


def test_simulate_scaleout_trades_reports_still_open_after_only_one_exit():
    events = [
        make_event(Direction.BUY, 100.0, datetime(2026, 1, 1)),
        make_event(Direction.SELL, 90.0, datetime(2026, 1, 5)),  # 只賣了一半，還缺第二次出場
    ]

    trades, still_open = simulate_scaleout_trades(events)

    assert trades == []
    assert still_open["entry"].price == 100.0
    assert len(still_open["exits"]) == 1


def test_simulate_scaleout_trades_reports_still_open_with_no_exit_at_all():
    events = [make_event(Direction.BUY, 100.0, datetime(2026, 1, 1))]

    trades, still_open = simulate_scaleout_trades(events)

    assert trades == []
    assert still_open["entry"].price == 100.0
    assert still_open["exits"] == []


def test_summarize_trades_works_on_scaleout_trades_too():
    events = [
        make_event(Direction.BUY, 100.0, datetime(2026, 1, 1)),
        make_event(Direction.SELL, 120.0, datetime(2026, 1, 5)),
        make_event(Direction.SELL, 140.0, datetime(2026, 1, 8)),
    ]
    trades, _ = simulate_scaleout_trades(events)

    summary = summarize_trades(trades)

    assert summary["n"] == 1
    assert summary["win_rate"] == 100.0
    assert summary["avg_return_pct"] == 30.0, "均價出場130 vs 進場100，(130-100)/100*100"
