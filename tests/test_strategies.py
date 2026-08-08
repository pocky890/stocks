import math

import pandas as pd

from stocks.models import Direction
from stocks.strategies.atr_breakout import ATRBreakoutStrategy
from stocks.strategies.bollinger import BollingerStrategy
from stocks.strategies.breakout import BreakoutStrategy
from stocks.strategies.chip_momentum import ChipMomentumStrategy
from stocks.strategies.golden_cross_scaleout import GoldenCrossScaleOutStrategy
from stocks.strategies.institutional_streak import InstitutionalStreakStrategy
from stocks.strategies.kd_strategy import KDStrategy
from stocks.strategies.long_swing import LongSwingStrategy
from stocks.strategies.ma_alignment import MAAlignmentStrategy
from stocks.strategies.ma_crossover import MACrossoverStrategy
from stocks.strategies.ma_trend import MATrendStrategy
from stocks.strategies.macd_strategy import MACDStrategy
from stocks.strategies.price_alert import PriceAlertStrategy
from stocks.strategies.rsi_mean_reversion import RSIMeanReversionStrategy
from stocks.strategies.rsi_strategy import RSIStrategy
from stocks.strategies.trend_following import TrendFollowingStrategy
from stocks.strategies.trust_momentum import TrustMomentumStrategy
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


def test_trust_momentum_enters_on_flexible_buy_window_and_exits_on_atr_stop():
    # 2026-08-08主訊號條件改成「近5日內至少3天買超、且5日淨額加總為正」(不要求連續)，
    # 用回測驗證過比原本的「連續3天」表現更好才採用。這組trust_net在idx2-4買超、idx0-1
    # 沒買超：idx4時「近5日(idx0-4)」有3天買超(idx2,3,4)、淨額加總15>0，條件成立且是
    # 這次才剛滿足(idx3時只有2天買超，還不到3天)，所以idx4是第一次進場的edge。
    closes = [10, 9, 10, 9, 10, 15, 18, 20, 8]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    trust_net = [0, 0, 5, 5, 5, 0, 0, 0, 0]
    bars = make_bars(closes, highs=highs, lows=lows)
    bars["trust_net"] = trust_net

    events = TrustMomentumStrategy().evaluate(
        "2330",
        bars,
        {"chip_window_days": 5, "chip_min_buy_days": 3, "rsi_period": 2, "rsi_overbought": 70, "atr_period": 2, "atr_multiplier": 2},
    )

    assert len(events) == 2
    buy, sell = events
    assert buy.direction == Direction.BUY
    assert buy.price == 10
    assert buy.ts == bars.index[4]
    assert sell.direction == Direction.SELL
    assert sell.price == 8
    assert sell.ts == bars.index[8]


def test_trust_momentum_returns_nothing_without_institutional_columns():
    bars = make_bars([10, 11, 12])
    assert TrustMomentumStrategy().evaluate("2330", bars, {}) == []


def test_long_swing_enters_on_regime_start_and_exits_on_ma_break():
    # 前14天10/11交錯震盪，建立RSI(14)歷史(讓RSI在進場當下維持中性50，不會被超買濾網擋掉)，
    # 不影響進出場判斷(外資淨額全0，籌碼條件不成立，regime即使中途亂跳也進不了場)。
    # 接著5天持平、再連漲3天，3日均線在idx19第一次穿越5日均線(regime啟動)且股價站上3日均線，
    # 同時外資最近2天(idx18-19)累計買超為正，構成首次進場的完整條件。
    # 之後急跌，連續2天收盤跌破3日均線觸發出場(atr_multiplier設100讓ATR停損不會提前介入，
    # 單獨驗證均線出場這條件)。
    closes = [10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10, 10, 10, 10, 10, 11, 12, 13, 8, 7, 6]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    foreign_net = [0] * 18 + [5, 5] + [0] * 5
    bars = make_bars(closes, highs=highs, lows=lows)
    bars["foreign_net"] = foreign_net

    events = LongSwingStrategy().evaluate(
        "2330",
        bars,
        {
            "trend_fast": 3,
            "trend_slow": 5,
            "atr_period": 2,
            "atr_multiplier": 100,
            "chip_lookback_days": 2,
            "exit_confirm_days": 2,
            "rsi_overbought": 70,
        },
    )

    assert len(events) == 2
    buy, sell = events
    assert buy.direction == Direction.BUY
    assert buy.price == 11
    assert buy.ts == bars.index[19]
    assert "首次進場" in buy.detail
    assert sell.direction == Direction.SELL
    assert sell.price == 7
    assert sell.ts == bars.index[23]
    assert "連續2天跌破3日均線" in sell.detail


def test_long_swing_returns_nothing_without_institutional_columns():
    bars = make_bars([10, 11, 12])
    assert LongSwingStrategy().evaluate("2330", bars, {}) == []


def test_trend_following_enters_on_ma_alignment_plus_volume_and_exits_on_ma_break():
    # 前4天持平(2/4日均線都還是10，尚未多頭排列)，接著價格連漲3天且量放大，2日均線
    # 站上4日均線且爆量觸發進場；最後急跌，收盤跌破2日均線(13)觸發出場(還沒跌破停損7)。
    closes = [10, 10, 10, 10, 12, 14, 16, 10]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    volumes = [1000, 1000, 1000, 1000, 3000, 3000, 3000, 1000]
    bars = make_bars(closes, volumes, highs=highs, lows=lows)

    events = TrendFollowingStrategy().evaluate(
        "2330", bars, {"fast": 2, "slow": 4, "volume_avg_period": 2, "atr_period": 2, "atr_multiplier": 2}
    )

    assert len(events) == 2
    buy, sell = events
    assert buy.direction == Direction.BUY
    assert buy.price == 12
    assert sell.direction == Direction.SELL
    assert sell.price == 10
    assert "跌破2日均線" in sell.detail
    assert buy.ts < sell.ts


def test_trend_following_stays_flat_generates_no_signal():
    closes = [10] * 8
    bars = make_bars(closes, highs=[11] * 8, lows=[9] * 8)
    events = TrendFollowingStrategy().evaluate(
        "2330", bars, {"fast": 2, "slow": 4, "volume_avg_period": 2, "atr_period": 2, "atr_multiplier": 2}
    )
    assert events == []


def test_rsi_mean_reversion_enters_on_oversold_bounce_and_exits_on_reverting_above_ma():
    # 前4天持平建立均線/布林基準，接著急跌到RSI(2)<10且跌破布林下軌觸發進場，之後反彈
    # 收盤站回4日均線觸發出場(用+-3的高低點確保近2日最低/ATR停損都還沒被跌破，單獨驗證
    # 均值回歸出場這條件)。
    closes = [20, 20, 20, 20, 18, 15, 12, 16, 20, 22]
    highs = [c + 3 for c in closes]
    lows = [c - 3 for c in closes]
    bars = make_bars(closes, highs=highs, lows=lows)

    events = RSIMeanReversionStrategy().evaluate(
        "2330",
        bars,
        {
            "rsi_period": 2,
            "rsi_oversold": 10,
            "rsi_overbought": 70,
            "bollinger_period": 4,
            "bollinger_num_std": 1,
            "ma_period": 4,
            "low_lookback_days": 2,
            "atr_period": 2,
            "atr_multiplier": 2,
        },
    )

    assert len(events) == 2
    buy, sell = events
    assert buy.direction == Direction.BUY
    assert buy.price == 18
    assert sell.direction == Direction.SELL
    assert sell.price == 16
    assert "站回4日均線" in sell.detail
    assert buy.ts < sell.ts


def test_rsi_mean_reversion_stays_flat_generates_no_signal():
    closes = [20] * 8
    bars = make_bars(closes, highs=[23] * 8, lows=[17] * 8)
    events = RSIMeanReversionStrategy().evaluate("2330", bars, {"rsi_period": 2, "bollinger_period": 4})
    assert events == []


def test_breakout_enters_on_donchian_high_plus_volume_and_exits_on_donchian_low():
    # 前3天持平建立唐奇安通道基準，第4天爆量創3日新高觸發進場；後面急漲又急跌，
    # 最後跌破前3日最低(14)出場(還沒跌破停損7)。
    closes = [10, 10, 10, 15, 18, 20, 8]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    volumes = [1000, 1000, 1000, 3000, 1000, 1000, 1000]
    bars = make_bars(closes, volumes, highs=highs, lows=lows)

    events = BreakoutStrategy().evaluate(
        "2330",
        bars,
        {"high_lookback_days": 3, "low_lookback_days": 3, "volume_avg_period": 3, "volume_multiplier": 1.5, "atr_period": 2, "atr_multiplier": 2},
    )

    assert len(events) == 2
    buy, sell = events
    assert buy.direction == Direction.BUY
    assert buy.price == 15
    assert sell.direction == Direction.SELL
    assert sell.price == 8
    assert "跌破前3日最低" in sell.detail
    assert buy.ts < sell.ts


def test_breakout_stays_flat_generates_no_signal():
    closes = [10] * 8
    bars = make_bars(closes, highs=[11] * 8, lows=[9] * 8)
    events = BreakoutStrategy().evaluate(
        "2330", bars, {"high_lookback_days": 3, "low_lookback_days": 3, "volume_avg_period": 3, "volume_multiplier": 1.5}
    )
    assert events == []


SCALEOUT_PARAMS = {"fast": 3, "mid": 5, "slow": 7, "chip_lookback_days": 5, "high_lookback_days": 5, "volume_avg_period": 3}


def test_golden_cross_scaleout_enters_on_full_score_then_exits_in_two_stages_on_separate_days():
    # 前7天持平建立均線/唐奇安通道基準，第8天5個打分條件全部到齊(MA3>MA7、站上MA7、
    # 近5日籌碼淨買超、突破前5日新高、量能放大)，滿分8分遠超過門檻5分觸發進場；之後
    # 價格衝高反轉緩跌：先放量跌破3日均線賣一半，隔一天才跌破5日均線賣掉剩餘一半。
    closes = [10, 10, 10, 10, 10, 10, 10, 12, 14, 16, 18, 20, 18, 16, 14, 12, 10, 8]
    volumes = [1000] * 7 + [3000, 1000, 1000, 1000, 1000, 3000, 1000, 1000, 1000, 1000, 1000]
    bars = make_bars(closes, volumes)
    bars["foreign_net"] = [0, 0, 0, 0, 50, 50, 50] + [0] * 11
    bars["trust_net"] = [0] * len(closes)

    events = GoldenCrossScaleOutStrategy().evaluate("2330", bars, SCALEOUT_PARAMS)

    assert len(events) == 3
    buy, sell_half, sell_rest = events
    assert buy.direction == Direction.BUY
    assert buy.price == 12
    assert "打分8分" in buy.detail
    assert sell_half.direction == Direction.SELL
    assert sell_half.price == 18
    assert "跌破3日均線且量能放大" in sell_half.detail
    assert "賣出一半" in sell_half.detail
    assert sell_rest.direction == Direction.SELL
    assert sell_rest.price == 16
    assert "跌破5日均線" in sell_rest.detail
    assert "賣出剩餘一半" in sell_rest.detail
    assert buy.ts < sell_half.ts < sell_rest.ts


def test_golden_cross_scaleout_enters_on_partial_score_reaching_threshold():
    # 沒有爆量、也沒有創新高(highs設得很高讓突破永遠不成立)，但MA3>MA7(+2)+站上MA7(+1)+
    # 籌碼買超(+2)=5分剛好達標，證明打分制不需要5個條件同時到齊，跟舊版4條件AND邏輯不同。
    closes = [10, 10, 10, 10, 10, 10, 10, 12, 14, 16, 18, 20, 18, 16, 14, 12, 10, 8]
    volumes = [1000] * len(closes)
    bars = make_bars(closes, volumes, highs=[50] * len(closes))
    bars["foreign_net"] = [0, 0, 0, 0, 50, 50, 50] + [0] * 11
    bars["trust_net"] = [0] * len(closes)

    events = GoldenCrossScaleOutStrategy().evaluate("2330", bars, SCALEOUT_PARAMS)

    buys = [e for e in events if e.direction == Direction.BUY]
    assert len(buys) == 1
    assert buys[0].price == 12
    assert "打分5分" in buys[0].detail
    assert "量增" not in buys[0].detail
    assert "突破" not in buys[0].detail


def test_golden_cross_scaleout_blocked_when_score_below_threshold():
    # 同上但完全沒有籌碼支撐，只剩MA3>MA7(+2)+站上MA7(+1)=3分，沒到5分門檻，不該進場。
    closes = [10, 10, 10, 10, 10, 10, 10, 12, 14, 16, 18, 20, 18, 16, 14, 12, 10, 8]
    bars = make_bars(closes, [1000] * len(closes), highs=[50] * len(closes))
    bars["foreign_net"] = [0] * len(closes)
    bars["trust_net"] = [0] * len(closes)

    events = GoldenCrossScaleOutStrategy().evaluate("2330", bars, SCALEOUT_PARAMS)
    assert events == []


def test_golden_cross_scaleout_sells_both_halves_same_day_on_gap_down():
    # 進場後價格直接跳空崩跌，同一天就跌破3日線跟5日線兩條均線，兩次出場事件應該同一天觸發。
    closes = [10, 10, 10, 10, 10, 10, 10, 12, 14, 16, 18, 20, 2]
    volumes = [1000] * 7 + [3000, 1000, 1000, 1000, 1000, 3000]
    bars = make_bars(closes, volumes)
    bars["foreign_net"] = [0, 0, 0, 0, 50, 50, 50] + [0] * 6
    bars["trust_net"] = [0] * len(closes)

    events = GoldenCrossScaleOutStrategy().evaluate("2330", bars, SCALEOUT_PARAMS)

    assert len(events) == 3
    sells = [e for e in events if e.direction == Direction.SELL]
    assert len(sells) == 2
    assert sells[0].ts == sells[1].ts == bars.index[-1]
    assert sells[0].price == sells[1].price == 2
    assert "賣出剩餘一半" in sells[1].detail


def test_golden_cross_scaleout_returns_nothing_without_institutional_columns():
    # 沒有籌碼欄位時chip_backed整段是False，缺了+2分；把突破也擋掉(highs設高)的話剩下
    # MA3>MA7(+2)+站上MA7(+1)+量增(+1)=4分不到5分門檻，不該進場。
    closes = [10, 10, 10, 10, 10, 10, 10, 12, 14, 16, 18, 20, 18, 16, 14, 12, 10, 8]
    volumes = [1000] * 7 + [3000, 1000, 1000, 1000, 1000, 3000, 1000, 1000, 1000, 1000, 1000]
    bars = make_bars(closes, volumes, highs=[50] * len(closes))
    events = GoldenCrossScaleOutStrategy().evaluate("2330", bars, SCALEOUT_PARAMS)
    assert events == []


def test_golden_cross_scaleout_rsi_filter_blocks_entry_when_overbought():
    # 20天持平後單邊上漲，量能在黃金交叉當天放大：MA3>MA7(+2)+站上MA7(+1)+量增(+1)=4分，
    # 沒有籌碼、也沒有突破(highs設高擋掉)，剛好卡在門檻前一分——這一分要靠RSI濾網補。
    # 這段單邊漲勢的RSI(14)算出來是100(超買)，用預設rsi_overbought=70會被擋住，但如果
    # 把rsi_overbought調高到200(等於這條濾網永遠算過)，同一筆資料就能補上第5分進場——
    # 證明RSI濾網真的在影響進場，不是擺著沒用的參數。
    closes = [10] * 20 + [10.5, 11, 11.5, 12, 12.5, 13, 13.5, 14]
    volumes = [1000] * 20 + [3000] + [1000] * 7
    bars = make_bars(closes, volumes, highs=[50] * len(closes))
    params = dict(SCALEOUT_PARAMS)

    blocked = GoldenCrossScaleOutStrategy().evaluate("2330", bars, {**params, "rsi_overbought": 70})
    assert blocked == [], "RSI(14)在這段單邊漲勢算出來是100，超買濾網該擋住這筆進場"

    allowed = GoldenCrossScaleOutStrategy().evaluate("2330", bars, {**params, "rsi_overbought": 200})
    buys = [e for e in allowed if e.direction == Direction.BUY]
    assert len(buys) == 1, "把超買門檻拉高到RSI永遠過關，同一筆資料應該能補上第5分進場"
    assert buys[0].price == 10.5
    assert "RSI未超買" in buys[0].detail
