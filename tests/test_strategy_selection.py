import pandas as pd

from stocks.notifier import NOTIFIABLE_STRATEGIES
from stocks.strategy_selection import (
    MIN_AVG_RETURN_PCT,
    MIN_PROFIT_FACTOR,
    MIN_TOTAL_RETURN_PCT,
    MIN_TRADES_FOR_RANKING,
    MIN_TRADES_OVERRIDES,
    compute_disabled_strategies,
    should_disable,
)


def make_summary(
    n=10, win_rate=50.0, avg_return_pct=1.0, total_return_pct=100.0, avg_return_excluding_best_pct=None, profit_factor=None
) -> dict:
    return {
        "n": n,
        "win_rate": win_rate,
        "avg_return_pct": avg_return_pct,
        "total_return_pct": total_return_pct,
        "avg_return_excluding_best_pct": avg_return_excluding_best_pct,
        "profit_factor": profit_factor,
    }


def test_should_disable_true_when_no_summary():
    # 2026-08-08使用者確認：完全沒有完整買賣配對，樣本等於0，一樣不可信，該保守排除，
    # 不是預設保留。
    assert should_disable(None) is True


def test_should_disable_true_when_too_few_trades_even_with_good_stats():
    # 樣本太少，勝率/報酬率本身就不可信——即使數字好看，一樣該保守排除，不拿雜訊當
    # 依據推播通知；等累積到MIN_TRADES_FOR_RANKING筆才開始真正判斷。
    summary = make_summary(n=MIN_TRADES_FOR_RANKING - 1, win_rate=100.0, avg_return_pct=50.0, total_return_pct=200.0)
    assert should_disable(summary) is True


def test_should_disable_false_for_low_win_rate_with_strong_positive_average():
    # 低勝率+高賺賠比是趨勢跟隨策略的正常樣貌，不該單獨用勝率否決——只要樣本夠、平均
    # 報酬跟加總報酬都夠好，就該保留
    summary = make_summary(
        n=MIN_TRADES_FOR_RANKING, win_rate=25.0, avg_return_pct=10.0, total_return_pct=100.0, avg_return_excluding_best_pct=3.0
    )
    assert should_disable(summary) is False


def test_should_disable_true_when_avg_return_below_cost_threshold_even_with_good_win_rate():
    # 樣本足夠、勝率不錯，但平均報酬率沒蓋過門檻，一樣該排除，不是只看勝率
    summary = make_summary(n=MIN_TRADES_FOR_RANKING, win_rate=70.0, avg_return_pct=MIN_AVG_RETURN_PCT - 0.1, total_return_pct=100.0)
    assert should_disable(summary) is True


def test_should_disable_true_when_total_return_below_threshold_even_with_good_average():
    # 2026-08-17使用者新增的門檻：平均報酬過關，但加總報酬沒超過MIN_TOTAL_RETURN_PCT，
    # 代表這段時間累積下來對整體貢獻不夠，一樣要排除，不能只看平均報酬
    summary = make_summary(
        n=MIN_TRADES_FOR_RANKING, win_rate=60.0, avg_return_pct=MIN_AVG_RETURN_PCT + 5.0, total_return_pct=MIN_TOTAL_RETURN_PCT
    )
    assert should_disable(summary) is True, "加總報酬剛好等於門檻(<=)也該排除，不是只有嚴格小於才排除"


def test_should_disable_false_when_both_avg_and_total_return_clear_threshold():
    summary = make_summary(
        n=MIN_TRADES_FOR_RANKING,
        win_rate=50.0,
        avg_return_pct=MIN_AVG_RETURN_PCT,
        total_return_pct=MIN_TOTAL_RETURN_PCT + 0.1,
    )
    assert should_disable(summary) is False


def test_should_disable_false_even_when_positive_average_relies_entirely_on_the_single_best_trade():
    # 2026-08-08使用者指出：拿掉單筆最賺的那一筆之後轉負，不該自動排除——「靠少數幾筆
    # 大波段撐報酬」正是這類趨勢跟隨策略設計上要抓的樣貌，不是瑕疵。只要樣本足夠、平均
    # 跟加總報酬都蓋過門檻，就該保留，不管扣掉最佳單筆後剩多少。
    summary = make_summary(
        n=MIN_TRADES_FOR_RANKING, win_rate=40.0, avg_return_pct=8.0, total_return_pct=100.0, avg_return_excluding_best_pct=-2.0
    )
    assert should_disable(summary) is False


def test_should_disable_uses_per_strategy_trade_override():
    # 2026-08-08使用者確認：long_swing持倉數月，交易天生比其他策略少，用同一套5筆門檻
    # 某些個股要等好幾年才達標，所以long_swing自己的門檻降到3筆——樣本剛好等於override
    # 門檻就該通過(只要平均報酬跟加總報酬也過關)，不是繼續套用全域的MIN_TRADES_FOR_RANKING。
    override_n = MIN_TRADES_OVERRIDES["long_swing"]
    summary = make_summary(n=override_n, win_rate=50.0, avg_return_pct=MIN_AVG_RETURN_PCT, total_return_pct=MIN_TOTAL_RETURN_PCT + 10)
    assert should_disable(summary, "long_swing") is False
    # 沒有strategy_name(或是其他沒被override的策略)一樣要用全域門檻擋下來
    assert should_disable(summary) is True
    assert should_disable(summary, "chip_momentum") is True


def test_should_disable_true_when_profit_factor_below_threshold_even_with_good_average():
    # 2026-08-17使用者拿10年真實資料比較後決定：獲利因子<MIN_PROFIT_FACTOR就排除，
    # 即使平均/加總報酬都過關——同一支股票同一種MDD深淺，獲利因子不夠代表賺的錢沒有
    # 明顯蓋過賠的錢，比單獨用MDD當門檻更準。
    summary = make_summary(
        n=MIN_TRADES_FOR_RANKING,
        win_rate=50.0,
        avg_return_pct=MIN_AVG_RETURN_PCT + 10,
        total_return_pct=MIN_TOTAL_RETURN_PCT + 100,
        profit_factor=MIN_PROFIT_FACTOR - 0.1,
    )
    assert should_disable(summary) is True


def test_should_disable_false_when_profit_factor_is_none_meaning_no_losing_trades():
    # profit_factor是None代表完全沒有虧損(分母是0)，是最好的情況，不該被當成排除依據
    summary = make_summary(
        n=MIN_TRADES_FOR_RANKING,
        avg_return_pct=MIN_AVG_RETURN_PCT + 10,
        total_return_pct=MIN_TOTAL_RETURN_PCT + 100,
        profit_factor=None,
    )
    assert should_disable(summary) is False


def test_should_disable_false_when_profit_factor_clears_threshold():
    summary = make_summary(
        n=MIN_TRADES_FOR_RANKING,
        avg_return_pct=MIN_AVG_RETURN_PCT + 10,
        total_return_pct=MIN_TOTAL_RETURN_PCT + 100,
        profit_factor=MIN_PROFIT_FACTOR,
    )
    assert should_disable(summary) is False


def test_compute_disabled_strategies_disables_everything_on_flat_data_with_no_signals():
    # 2026-08-08使用者確認：完全持平的資料，NOTIFIABLE_STRATEGIES都不會有任何完整買賣
    # 配對(樣本等於0)，該排除全部策略——這正是新增股票剛加進來、歷史資料還很少時的
    # 真實情境，新股票會先整組安靜，等資料累積夠了下次重跑才會開始有判斷結果。
    closes = [50] * 10
    dates = pd.date_range("2026-01-02", periods=len(closes), freq="D")
    bars = pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": [1000] * len(closes)},
        index=dates,
    )

    assert set(compute_disabled_strategies("2454", bars, {})) == NOTIFIABLE_STRATEGIES
