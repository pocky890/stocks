import math

import pandas as pd

from stocks.models import Direction
from stocks.strategies.atr_breakout import ATRBreakoutStrategy
from stocks.strategies.bollinger import BollingerStrategy
from stocks.strategies.chip_momentum import ChipMomentumStrategy
from stocks.strategies.composite_formula import BuyFormulaStrategy, SellFormulaStrategy, compute_buy_condition
from stocks.strategies.institutional_streak import InstitutionalStreakStrategy
from stocks.strategies.kd_strategy import KDStrategy
from stocks.strategies.ma_alignment import MAAlignmentStrategy
from stocks.strategies.ma_crossover import MACrossoverStrategy
from stocks.strategies.ma_trend import MATrendStrategy
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


def test_kd_fires_golden_in_oversold_then_death_in_overbought():
    # a sine wave naturally cycles K/D through oversold and overbought zones with real crossings
    closes = [100 + 30 * math.sin(i / 6) for i in range(60)]
    bars = make_bars(closes)
    events = KDStrategy().evaluate("2330", bars, {"rsv_period": 9, "k_smooth": 3, "d_smooth": 3})

    buys = [e for e in events if e.direction == Direction.BUY]
    sells = [e for e in events if e.direction == Direction.SELL]
    assert len(buys) == 1, "golden cross only counts while K and D are both under the oversold threshold"
    assert len(sells) == 1, "death cross only counts while K and D are both over the overbought threshold"
    assert buys[0].ts < sells[0].ts


def test_kd_ignores_crossings_outside_the_extreme_zones():
    # a small wiggle around the midline crosses K/D repeatedly but never enters <20 or >80
    closes = [100 + 3 * math.sin(i / 2) for i in range(40)]
    bars = make_bars(closes)
    events = KDStrategy().evaluate("2330", bars, {"rsv_period": 9, "k_smooth": 3, "d_smooth": 3})
    assert events == []


def test_institutional_streak_fires_once_when_threshold_first_reached():
    closes = [100] * 10
    bars = make_bars(closes)
    bars["foreign_net"] = [100, 100, -50, 200, 200, 200, 200, -10, -10, -10]
    bars["trust_net"] = [0] * 10

    events = InstitutionalStreakStrategy().evaluate("2330", bars, {"threshold_days": 3})

    foreign_buys = [e for e in events if e.detail == "外資連續3日買超"]
    assert len(foreign_buys) == 1, "streak of exactly 3 positive days should fire once, not keep re-firing"
    assert foreign_buys[0].ts == bars.index[5], "index 3,4,5 are the 1st/2nd/3rd positive day of that streak"


def test_institutional_streak_tracks_foreign_and_trust_independently():
    closes = [100] * 6
    bars = make_bars(closes)
    bars["foreign_net"] = [100, 100, 100, 100, 100, 100]  # foreign never breaks its buy streak
    bars["trust_net"] = [-50, -50, -50, 50, 50, 50]  # trust flips from selling to buying

    events = InstitutionalStreakStrategy().evaluate("2330", bars, {"threshold_days": 3})

    assert {(e.detail, e.direction) for e in events} == {
        ("外資連續3日買超", Direction.BUY),
        ("投信連續3日賣超", Direction.SELL),
        ("投信連續3日買超", Direction.BUY),
    }


def test_ma_trend_fires_once_when_price_and_slow_ma_slope_both_confirm():
    # flat around 5 (slow MA flat), then a sustained rise lifts price above both MAs and
    # turns the slow MA's own slope positive -- single edge event, no re-fire while it holds
    closes = [5, 5, 5, 5, 5] + [10, 12, 14, 16, 18, 20]
    bars = make_bars(closes)
    events = MATrendStrategy().evaluate("2330", bars, {"fast": 2, "slow": 4})

    assert len(events) == 1, "the AND-condition should be a single edge event, not one per day it holds"
    assert events[0].direction == Direction.BUY
    assert all(e.direction == Direction.BUY for e in events), "只定義了進場條件，不該出現SELL"


def test_ma_trend_waits_for_slow_ma_slope_even_if_price_already_above_both_mas():
    # a sharp bounce puts price back above both MAs a step before the slow (longer-lookback)
    # MA's own slope actually turns positive -- price position alone isn't enough
    closes = [30, 26, 22, 18, 14, 20, 21, 25]
    bars = make_bars(closes)
    events = MATrendStrategy().evaluate("2330", bars, {"fast": 2, "slow": 4})

    assert len(events) == 1
    assert events[0].ts == bars.index[7], "價格站上均線是在index6，但慢線斜率要到index7才轉正"


def test_buy_formula_fires_when_chip_support_trend_and_breakout_all_align():
    # flat baseline (25 days) then a rally that: lifts price above its 5/20-day MA (step 2),
    # breaks out above the Bollinger upper band on a volume spike (step 3A) -- all landing on
    # the same day foreign money completes a 2-day buying streak (step 1)
    closes = [50] * 25 + [52, 55, 60, 68, 80]
    volumes = [1000] * 29 + [6000]
    bars = make_bars(closes, volumes=volumes)
    bars["foreign_net"] = [0] * 28 + [100, 100]
    bars["trust_net"] = [0] * 30

    events = BuyFormulaStrategy().evaluate("2330", bars, {})

    assert len(events) == 1, "the 3-step AND-condition should be a single edge event"
    assert events[0].direction == Direction.BUY
    assert events[0].ts == bars.index[-1]
    assert "爆量突破布林上軌" in events[0].detail


def test_buy_formula_does_not_fire_without_institutional_chip_data():
    # identical setup to the passing case above, but with no foreign_net/trust_net columns --
    # the chip condition can never be satisfied, so the formula must never fire
    closes = [50] * 25 + [52, 55, 60, 68, 80]
    volumes = [1000] * 29 + [6000]
    bars = make_bars(closes, volumes=volumes)

    assert BuyFormulaStrategy().evaluate("2330", bars, {}) == []


def test_buy_condition_chip_check_counts_non_consecutive_buying_days():
    # 2026-08-06 adjustment: chip support is now a rolling "N buy-days out of the last
    # chip_lookback_days(5) days" count, not a consecutive streak -- two buy days with a gap
    # between them should still satisfy chip_min_days(2) as long as both fall inside the
    # trailing 5-day window on the day being checked. Everything else (mild uptrend so trend_up
    # holds throughout, a forced volume+price breakout on day 24) is identical between the two
    # variants -- only the chip timing differs, isolating exactly what's under test.
    closes = [100 + i * 0.3 for i in range(30)]
    closes[24] += 5  # force a bollinger breakout on day 24
    volumes = [1000] * 30
    volumes[24] = 5000  # force a volume spike on day 24

    def build(foreign_buy_days):
        bars = make_bars(closes, volumes=volumes)
        foreign_net = [0] * 30
        for d in foreign_buy_days:
            foreign_net[d] = 100
        bars["foreign_net"] = foreign_net
        bars["trust_net"] = [0] * 30
        return bars

    within_window, _ = compute_buy_condition(build([20, 24]), {})
    outside_window, _ = compute_buy_condition(build([15, 24]), {})

    assert bool(within_window.iloc[24]), "day20+day24 both fall in the trailing 5-day window ending day24"
    assert not bool(outside_window.iloc[24]), "day15 is outside day24's trailing 5-day window -- only 1 buy day counts"


def test_sell_formula_fires_via_ma5_plus_rsi_confirmation():
    # a rally pushes RSI persistently high (it decays slowly), then a short decline drops the
    # price below its own 5-day MA while RSI is still >80 -- condition 1 (5-day MA breach
    # confirmed by RSI overbought)
    uptrend = list(range(1, 20))
    downtrend = list(range(18, 5, -1))
    bars = make_bars(uptrend + downtrend)

    events = SellFormulaStrategy().evaluate("2330", bars, {})

    assert len(events) == 1, "the OR-condition should be a single edge event"
    assert "跌破5日均線+RSI超買" in events[0].detail
    assert events[0].direction == Direction.SELL


def test_sell_formula_fires_via_ma10_alone_without_rsi_or_chip_confirmation():
    # a gentle oscillation keeps RSI moderate (never overbought) with no institutional data,
    # then a mild decline drags price below its own 10-day MA -- condition 2 fires on its own,
    # with neither of condition 1's confirming factors present
    closes = [100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100, 99, 97, 95, 93, 91]
    bars = make_bars(closes)

    events = SellFormulaStrategy().evaluate("2330", bars, {})

    assert len(events) >= 1
    assert all("跌破10日均線" in e.detail and "RSI超買" not in e.detail for e in events)


def test_sell_formula_does_not_fire_on_ma5_breach_alone_without_confirmation():
    # price dips below its 5-day MA early in a mild pullback, but RSI is nowhere near
    # overbought and there's no institutional data -- condition 1 needs a confirming factor,
    # condition 2 (10-day MA) isn't reached either, so nothing should fire yet
    bars = make_bars([100, 100, 100, 100, 100, 99, 98])

    assert SellFormulaStrategy().evaluate("2330", bars, {}) == []


def test_sell_formula_fires_via_institutional_selling_confirmation():
    # a gentle sawtooth rise (keeps RSI moderate, not pinned at an artificial 100) then a
    # 1-day dip below the 5-day MA, landing on the same day a 3-day foreign-selling streak
    # completes -- RSI stays at 54.5 that day, isolating the institutional-selling branch
    # of condition 1 from the RSI branch
    closes = [100, 102, 101, 103, 102, 104, 103, 105, 104, 106, 105, 107, 106, 108, 107, 104]
    bars = make_bars(closes)
    n = len(closes)
    bars["foreign_net"] = [0] * (n - 3) + [-50, -50, -50]
    bars["trust_net"] = [0] * n

    events = SellFormulaStrategy().evaluate("2330", bars, {})

    assert len(events) == 1
    assert events[0].ts == bars.index[-1]
    assert "法人連續賣超" in events[0].detail
    assert "RSI超買" not in events[0].detail, "當天RSI只有54.5，不該算超買"


def test_institutional_streak_returns_nothing_without_institutional_columns():
    bars = make_bars([100, 101, 102])
    assert InstitutionalStreakStrategy().evaluate("2330", bars, {"threshold_days": 3}) == []


def test_atr_breakout_enters_on_donchian_breakout_and_trails_stop_up_before_exiting():
    # flat first 3 days establish the donchian channel + ATR baseline, then a breakout
    # above the prior 3-day high buys in; price keeps rising (stop ratchets up: 7 -> 8 -> 13)
    # before a sharp drop finally breaches the trailing stop and exits.
    closes = [10, 10, 10, 15, 18, 20, 8]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    bars = make_bars(closes, highs=highs, lows=lows)

    events = ATRBreakoutStrategy().evaluate(
        "2330", bars, {"donchian_period": 3, "atr_period": 2, "atr_multiplier": 2}
    )

    assert len(events) == 2
    buy, sell = events
    assert buy.direction == Direction.BUY
    assert buy.price == 15
    assert sell.direction == Direction.SELL
    assert sell.price == 8
    assert buy.ts < sell.ts


def test_atr_breakout_stays_flat_generates_no_signal():
    closes = [10] * 8
    bars = make_bars(closes, highs=[11] * 8, lows=[9] * 8)
    events = ATRBreakoutStrategy().evaluate(
        "2330", bars, {"donchian_period": 3, "atr_period": 2, "atr_multiplier": 2}
    )
    assert events == []


def test_chip_momentum_enters_on_foreign_buy_streak_and_exits_on_atr_stop():
    # 前5天10/9交錯震盪(RSI維持中性，不會被「未超買」濾網擋掉)，外資在idx2-4連續3天買超
    # (streak在idx4達到3天門檻)觸發進場；之後價格急漲又急跌，ATR停損線跟著往上拉再被跌破出場。
    closes = [10, 9, 10, 9, 10, 15, 18, 20, 8]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    foreign_net = [0, 0, 5, 5, 5, 0, 0, 0, 0]
    bars = make_bars(closes, highs=highs, lows=lows)
    bars["foreign_net"] = foreign_net

    events = ChipMomentumStrategy().evaluate(
        "2330", bars, {"chip_streak_days": 3, "rsi_period": 2, "rsi_overbought": 70, "atr_period": 2, "atr_multiplier": 2}
    )

    assert len(events) == 2
    buy, sell = events
    assert buy.direction == Direction.BUY
    assert buy.price == 10
    assert buy.ts == bars.index[4]
    assert sell.direction == Direction.SELL
    assert sell.price == 8
    assert sell.ts == bars.index[8]


def test_chip_momentum_returns_nothing_without_institutional_columns():
    bars = make_bars([10, 11, 12])
    assert ChipMomentumStrategy().evaluate("2330", bars, {}) == []
