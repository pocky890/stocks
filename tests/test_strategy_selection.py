import pandas as pd

from stocks.strategy_selection import (
    MIN_AVG_RETURN_PCT,
    MIN_TRADES_FOR_RANKING,
    WIN_RATE_THRESHOLD,
    compute_disabled_strategies,
    should_disable,
)


def make_summary(n=10, win_rate=50.0, avg_return_pct=1.0) -> dict:
    return {"n": n, "win_rate": win_rate, "avg_return_pct": avg_return_pct}


def test_should_disable_false_when_no_summary():
    assert should_disable(None) is False


def test_should_disable_false_when_too_few_trades_even_with_bad_stats():
    # 樣本太少，勝率/報酬率本身就不可信，不該拿噪音做排除決定
    summary = make_summary(n=MIN_TRADES_FOR_RANKING - 1, win_rate=0.0, avg_return_pct=-50.0)
    assert should_disable(summary) is False


def test_should_disable_true_when_win_rate_below_threshold():
    summary = make_summary(win_rate=WIN_RATE_THRESHOLD - 1, avg_return_pct=10.0)
    assert should_disable(summary) is True


def test_should_disable_true_when_avg_return_below_cost_threshold_even_with_good_win_rate():
    # 勝率不錯但平均報酬率沒蓋過交易成本，一樣該排除，不是只看勝率
    summary = make_summary(win_rate=70.0, avg_return_pct=MIN_AVG_RETURN_PCT - 0.1)
    assert should_disable(summary) is True


def test_should_disable_false_when_both_thresholds_cleared():
    summary = make_summary(win_rate=WIN_RATE_THRESHOLD, avg_return_pct=MIN_AVG_RETURN_PCT)
    assert should_disable(summary) is False


def test_compute_disabled_strategies_returns_empty_list_on_flat_data_with_no_signals():
    # 完全持平的資料，NOTIFIABLE_STRATEGIES都不會有任何完整買賣配對(樣本不足)，
    # 不該排除任何策略——這正是新增股票剛加進來、歷史資料還很少時的真實情境。
    closes = [50] * 10
    dates = pd.date_range("2026-01-02", periods=len(closes), freq="D")
    bars = pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": [1000] * len(closes)},
        index=dates,
    )

    assert compute_disabled_strategies("2454", bars, {}) == []
