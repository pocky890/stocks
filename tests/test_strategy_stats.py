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


def test_summarize_trades_computes_profit_factor_as_gains_over_losses():
    # 2026-08-17使用者(轉述Gemini建議)要求補上的風險指標：獲利因子=總獲利/總虧損(絕對值)，
    # 跟勝率是不同維度——這裡3筆賺(各+20%)、2筆賠(各-10%)，總獲利60/總虧損20
    events = [
        make_event(Direction.BUY, 100.0, datetime(2026, 1, 1)),
        make_event(Direction.SELL, 120.0, datetime(2026, 1, 2)),
        make_event(Direction.BUY, 100.0, datetime(2026, 1, 3)),
        make_event(Direction.SELL, 90.0, datetime(2026, 1, 4)),
        make_event(Direction.BUY, 100.0, datetime(2026, 1, 5)),
        make_event(Direction.SELL, 120.0, datetime(2026, 1, 6)),
        make_event(Direction.BUY, 100.0, datetime(2026, 1, 7)),
        make_event(Direction.SELL, 90.0, datetime(2026, 1, 8)),
        make_event(Direction.BUY, 100.0, datetime(2026, 1, 9)),
        make_event(Direction.SELL, 120.0, datetime(2026, 1, 10)),
    ]
    trades, _ = simulate_round_trips(events)

    summary = summarize_trades(trades)

    assert summary["profit_factor"] == pytest.approx(60 / 20)


def test_summarize_trades_profit_factor_is_none_when_no_losing_trades():
    # 完全沒有虧損時比值沒有意義(分母是0)，該回傳None，不是0或無限大
    events = [
        make_event(Direction.BUY, 100.0, datetime(2026, 1, 1)),
        make_event(Direction.SELL, 110.0, datetime(2026, 1, 2)),
    ]
    trades, _ = simulate_round_trips(events)

    summary = summarize_trades(trades)

    assert summary["profit_factor"] is None


def test_summarize_trades_computes_max_drawdown_from_cumulative_curve():
    # 依進場時間累加報酬率畫一條簡化權益曲線：+20,-30,+10,-5,+25 -> 累積20,-10,0,-5,20
    # 高點在20(第1筆)，最低點在-10(第2筆)，最大回撤=20-(-10)=30
    events = [
        make_event(Direction.BUY, 100.0, datetime(2026, 1, 1)),
        make_event(Direction.SELL, 120.0, datetime(2026, 1, 2)),  # +20%
        make_event(Direction.BUY, 100.0, datetime(2026, 1, 3)),
        make_event(Direction.SELL, 70.0, datetime(2026, 1, 4)),  # -30%
        make_event(Direction.BUY, 100.0, datetime(2026, 1, 5)),
        make_event(Direction.SELL, 110.0, datetime(2026, 1, 6)),  # +10%
        make_event(Direction.BUY, 100.0, datetime(2026, 1, 7)),
        make_event(Direction.SELL, 95.0, datetime(2026, 1, 8)),  # -5%
        make_event(Direction.BUY, 100.0, datetime(2026, 1, 9)),
        make_event(Direction.SELL, 125.0, datetime(2026, 1, 10)),  # +25%
    ]
    trades, _ = simulate_round_trips(events)

    summary = summarize_trades(trades)

    assert summary["max_drawdown_pct"] == pytest.approx(30.0)


def test_summarize_trades_max_drawdown_is_zero_when_curve_never_declines():
    events = [
        make_event(Direction.BUY, 100.0, datetime(2026, 1, 1)),
        make_event(Direction.SELL, 110.0, datetime(2026, 1, 2)),
        make_event(Direction.BUY, 100.0, datetime(2026, 1, 3)),
        make_event(Direction.SELL, 105.0, datetime(2026, 1, 4)),
    ]
    trades, _ = simulate_round_trips(events)

    summary = summarize_trades(trades)

    assert summary["max_drawdown_pct"] == 0.0


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
