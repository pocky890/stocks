import pandas as pd
import pytest

from stocks import db
from stocks.config import Config
from stocks.models import Bar
from stocks.watchlist_view import (
    _bollinger_text,
    _current_streak,
    _ma_price_text,
    _macd_text,
    _rsi_text,
    _round_or_none,
    _volume_text,
    build_overview_rows,
    change_text,
    compute_change,
    institutional_text,
    price_text,
)


def make_config(db_path: str) -> Config:
    return Config(
        shioaji_api_key="",
        shioaji_secret_key="",
        telegram_bot_token="",
        telegram_chat_id="",
        market_open="09:00",
        market_close="13:30",
        bar_interval_minutes=5,
        batch_pacing_seconds=0,
        strategy_params={
            "rsi": {"period": 14, "oversold": 30, "overbought": 70},
            "macd": {"fast": 12, "slow": 26, "signal": 9},
            "bollinger": {"period": 20, "num_std": 2},
            "volume_anomaly": {"avg_period": 20, "multiplier": 2},
            "kd": {"rsv_period": 9, "k_smooth": 3, "d_smooth": 3},
        },
        db_path=db_path,
    )


def test_round_or_none_strips_trailing_zero_decimal():
    assert _round_or_none(2025.0) == 2025
    assert isinstance(_round_or_none(2025.0), int)


def test_round_or_none_keeps_nonzero_decimal_to_one_place():
    assert _round_or_none(1739.53) == 1739.5


def test_round_or_none_passes_through_none():
    assert _round_or_none(None) is None


def test_rsi_text_boundaries():
    assert "超賣 (30)" in _rsi_text(29.9) and "color:red" in _rsi_text(29.9), "超賣=可能反彈=紅"
    assert "中性 (30)" in _rsi_text(30) and "color:inherit" in _rsi_text(30), "門檻上剛好不算超賣，中性不上色"
    assert "中性 (70)" in _rsi_text(70) and "color:inherit" in _rsi_text(70)
    assert "超買 (70)" in _rsi_text(70.1) and "color:green" in _rsi_text(70.1), "超買=可能回落=綠"
    assert _rsi_text(None) == "—"


def test_macd_text_sign():
    assert "多頭 (+2.3)" in _macd_text(2.3) and "color:red" in _macd_text(2.3)
    assert "空頭 (-1.5)" in _macd_text(-1.5) and "color:green" in _macd_text(-1.5)


def test_ma_price_text_colors_by_position_relative_to_close():
    assert "color:red" in _ma_price_text(latest_close=100, ma_series=pd.Series([90, 90])), "現價站上均線=多方"
    assert "color:green" in _ma_price_text(latest_close=100, ma_series=pd.Series([110, 110])), "現價跌破均線=空方"
    assert _ma_price_text(latest_close=100, ma_series=pd.Series([float("nan")])) == "—"


def test_ma_price_text_shows_arrow_for_ma_slope_not_price_position():
    assert "↑" in _ma_price_text(latest_close=100, ma_series=pd.Series([90, 92])), "均線比昨天高=上揚"
    assert "↓" in _ma_price_text(latest_close=100, ma_series=pd.Series([92, 90])), "均線比昨天低=下彎"
    text = _ma_price_text(latest_close=100, ma_series=pd.Series([90]))
    assert "↑" not in text and "↓" not in text, "只有一天資料算不出斜率，不該亂猜箭頭"


def test_bollinger_text_positions():
    assert _bollinger_text(close=100, upper=100, lower=80) == "接近上軌"
    assert _bollinger_text(close=80, upper=100, lower=80) == "接近下軌"
    assert _bollinger_text(close=90, upper=100, lower=80) == "中間"


def test_volume_text_threshold():
    assert _volume_text(2.5, multiplier=2) == "爆量 (2.5倍)"
    assert _volume_text(1.0, multiplier=2) == "正常 (1.0倍)"


def test_current_streak_counts_from_the_end():
    series = pd.Series([100, -50, 200, 200, 200])
    sign, length = _current_streak(series)
    assert sign == 1
    assert length == 3


def test_current_streak_ignores_trailing_nan_by_dropping_it():
    series = pd.Series([200, 200, 200, None])
    sign, length = _current_streak(series)
    assert sign == 1
    assert length == 3, "a missing latest day shouldn't erase yesterday's real streak"


def test_institutional_text_combines_both():
    foreign = pd.Series([100, 100, 100])
    trust = pd.Series([-50, -50, -50])
    text = institutional_text(foreign, trust)
    assert "外資：連買3日" in text and "color:red" in text, "連買達3日門檻要標紅提醒"
    assert "投信：連賣3日" in text and "color:green" in text, "連賣達3日門檻要標綠提醒"


def test_institutional_text_no_color_below_streak_threshold():
    foreign = pd.Series([100, 100])  # 只有2天，預設門檻是3天
    text = institutional_text(foreign, pd.Series(dtype=float))
    assert text == "外資：連買2日", "未達門檻的短streak不用上色"


def test_institutional_text_empty_when_no_streak():
    assert institutional_text(pd.Series(dtype=float), pd.Series(dtype=float)) == "—"


def test_build_overview_rows_end_to_end(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    config = make_config(db_path)

    closes = [100 + i * 0.5 for i in range(30)]
    dates = pd.date_range("2026-01-02", periods=30, freq="D")
    bars = [
        Bar(symbol="2330", ts=ts.to_pydatetime(), open=c, high=c + 1, low=c - 1, close=c, volume=1000)
        for ts, c in zip(dates, closes)
    ]

    with db.connect(db_path) as conn:
        db.insert_bars_daily(conn, bars)
        db.add_to_watchlist(conn, "2330", name="台積電")

    rows = build_overview_rows(config)

    assert len(rows) == 1
    row = rows[0]
    assert row["代號"] == "2330"
    assert row["名稱"] == "台積電"
    assert str(round(closes[-1], 1)) in row["目前價位"], "目前價位現在是帶顏色的HTML，不是裸數字"
    # 這批合成資料全部30天都在「今天」之前(今天沒有自己的bars_daily row)，
    # 所以「昨收」跟「目前價位」剛好都落在同一天(最後一天)上，不是巧合
    assert row["昨收"] == pytest.approx(closes[-1])
    assert row["5日"] is not None
    assert row["RSI"] != "—", "30 days of data should be enough for a 14-period RSI"
    assert row["三大法人"] == "—", "no institutional_flows rows inserted for this symbol"
    assert list(row["KD"].columns) == ["k", "d"]
    assert not row["KD"].empty, "30 days is enough history for KD to have values -- it should feed the mini chart, not be blank"
    assert len(row["KD"]) <= 20, "KD只給小圖看最近的走勢跟交叉，不用整段歷史"


def test_build_overview_rows_handles_symbol_with_no_bars(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    config = make_config(db_path)

    with db.connect(db_path) as conn:
        db.add_to_watchlist(conn, "9999", name="沒資料的股票")

    rows = build_overview_rows(config)

    assert rows[0]["代號"] == "9999"
    assert rows[0]["目前價位"] == "—"
    assert rows[0]["昨收"] is None
    assert rows[0]["RSI"] == "—"
    assert rows[0]["KD"].empty


def test_compute_change_falls_back_to_daily_close_when_no_intraday_data():
    # bars_daily already has today's row (e.g. daily_update ran) but no bars_5min yet
    today = pd.Timestamp.now().normalize()
    yesterday = today - pd.Timedelta(days=1)
    bars_daily = pd.DataFrame({"close": [95.0, 100.0]}, index=[yesterday, today])

    change, change_pct = compute_change(bars_daily, pd.DataFrame())

    assert change == pytest.approx(5.0)
    assert change_pct == pytest.approx(5.0 / 95 * 100)


def test_compute_change_prefers_todays_intraday_close():
    today = pd.Timestamp.now().normalize()
    yesterday = today - pd.Timedelta(days=1)
    bars_daily = pd.DataFrame({"close": [100.0]}, index=[yesterday])
    today_bars = pd.DataFrame({"close": [110.0]}, index=[today + pd.Timedelta(hours=10)])

    change, change_pct = compute_change(bars_daily, today_bars)

    assert change == pytest.approx(10.0)
    assert change_pct == pytest.approx(10.0)


def test_compute_change_returns_none_without_a_prior_trading_day():
    today = pd.Timestamp.now().normalize()
    bars_daily = pd.DataFrame({"close": [100.0]}, index=[today])

    change, change_pct = compute_change(bars_daily, pd.DataFrame())

    assert change is None
    assert change_pct is None


def test_change_text_colors_red_for_up_green_for_down():
    assert "color:red" in change_text(5.0, 5.3)
    assert "color:green" in change_text(-3.0, -2.9)
    assert change_text(None, None) == "—"


def test_price_text_colors_red_for_up_green_for_down():
    assert "color:red" in price_text(105.0, 2.0)
    assert "color:green" in price_text(95.0, -2.0)
    assert "105" in price_text(105.0, 2.0)


def test_price_text_highlights_limit_up_with_red_background():
    html = price_text(110.0, 10.0)
    assert "background-color:red" in html
    assert "110" in html


def test_price_text_highlights_limit_down_with_green_background():
    html = price_text(90.0, -9.8)
    assert "background-color:green" in html


def test_price_text_plain_number_when_change_unknown():
    assert price_text(105.0, None) == "105"


def test_price_text_passes_through_none():
    assert price_text(None, 2.0) == "—"
