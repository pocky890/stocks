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
    build_paper_trades,
    build_strategy_recommendations,
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


def test_ma_price_text_arrow_color_is_independent_from_number_color():
    # 現價跌破均線(數字該是綠色)，但均線本身還在上揚(箭頭該是紅色)——兩者是獨立維度，
    # 顏色不該綁在一起，避免像「1846.5↓」用紅字卻配綠箭頭這種看起來矛盾的組合
    text = _ma_price_text(latest_close=100, ma_series=pd.Series([105, 110]))
    assert 'color:green">110</span>' in text, "現價跌破均線，數字維持綠色"
    assert '<span style="color:red">↑</span>' in text, "均線本身上揚，箭頭獨立標紅"

    text = _ma_price_text(latest_close=100, ma_series=pd.Series([95, 90]))
    assert 'color:red">90</span>' in text, "現價站上均線，數字維持紅色"
    assert '<span style="color:green">↓</span>' in text, "均線本身下彎，箭頭獨立標綠"


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


def test_build_overview_rows_uses_fresh_intraday_price_consistently_with_change(tmp_path):
    # 2026-08-13發現的bug：bars_daily的「今天」列是daily_update盤中抓到、之後整天凍結
    # 的快照(2210)，但bars_5min還在即時更新(2260，4分鐘前)——「漲跌」欄位當時已經改用
    # 新鮮的bars_5min算對了，但「目前價位」/均線比較還是直接讀bars["close"]最後一筆
    # (凍結的2210)，兩欄數字對不起來。這裡驗證兩者現在用同一個現價。
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    config = make_config(db_path)

    today = pd.Timestamp.now().normalize()
    closes = [100 + i * 0.5 for i in range(20)] + [2025.0]  # 昨天收盤2025
    dates = pd.date_range(end=today - pd.Timedelta(days=1), periods=len(closes), freq="D")
    bars = [
        Bar(symbol="8299", ts=ts.to_pydatetime(), open=c, high=c + 1, low=c - 1, close=c, volume=1000)
        for ts, c in zip(dates, closes)
    ]
    frozen_snapshot = Bar(
        symbol="8299", ts=today.to_pydatetime(), open=2260, high=2260, low=2175, close=2210, volume=100
    )
    fresh_tick = Bar(
        symbol="8299",
        ts=(pd.Timestamp.now() - pd.Timedelta(minutes=4)).to_pydatetime(),
        open=2260, high=2265, low=2255, close=2260, volume=50,
    )

    with db.connect(db_path) as conn:
        db.insert_bars_daily(conn, bars + [frozen_snapshot])
        db.insert_bars_5min(conn, [fresh_tick])
        db.add_to_watchlist(conn, "8299", name="群聯")

    row = build_overview_rows(config)[0]

    assert "2260" in row["目前價位"], "該用還在更新的bars_5min現價(2260)，不是凍結的daily快照(2210)"
    assert "+235.0" in row["漲跌"], "跟「目前價位」用同一個現價算出來的漲跌，兩者不該對不起來"
    assert row["昨收"] == pytest.approx(2025.0)


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
    fresh_today_bars = pd.DataFrame({"close": [110.0]}, index=[pd.Timestamp.now() - pd.Timedelta(minutes=2)])

    change, change_pct = compute_change(bars_daily, fresh_today_bars)

    assert change == pytest.approx(10.0)
    assert change_pct == pytest.approx(10.0)


def test_compute_change_prefers_daily_close_over_stale_intraday_when_both_have_today():
    # bars_daily已經有今天這筆(daily_update跑過，抓到收盤或最新報價)，但bars_5min是
    # run_live.py早上開一段時間又停掉留下的舊資料(超過15分鐘沒更新)——這時該信bars_daily，
    # 不能讓已經停更新的盤中5分K蓋掉更完整/更新的日線收盤(這就是2026-08-07那次8299
    # 價格對不上的bug)
    today = pd.Timestamp.now().normalize()
    yesterday = today - pd.Timedelta(days=1)
    bars_daily = pd.DataFrame({"close": [2025.0, 2020.0]}, index=[yesterday, today])
    stale_today_bars = pd.DataFrame({"close": [2040.0]}, index=[pd.Timestamp.now() - pd.Timedelta(hours=2)])

    change, change_pct = compute_change(bars_daily, stale_today_bars)

    assert change == pytest.approx(-5.0)
    assert change_pct == pytest.approx(-5.0 / 2025 * 100)


def test_compute_change_prefers_fresh_intraday_over_stale_daily_snapshot_when_both_have_today():
    # 反過來的情境(2026-08-13發現)：bars_daily的「今天」列是daily_update盤中(例如上午)
    # 剛好跑過一次check_and_update時抓到的即時快照，之後整天不會再更新、凍結在那個
    # 時間點；但bars_5min是run_live.py持續累積的，現在還在更新(15分鐘內有新資料)——
    # 這時該信還在跳動的bars_5min，不能讓早上凍結的bars_daily快照蓋掉真正的現價
    today = pd.Timestamp.now().normalize()
    yesterday = today - pd.Timedelta(days=1)
    bars_daily = pd.DataFrame({"close": [2025.0, 2210.0]}, index=[yesterday, today])  # 上午的凍結快照
    fresh_today_bars = pd.DataFrame({"close": [2260.0]}, index=[pd.Timestamp.now() - pd.Timedelta(minutes=4)])

    change, change_pct = compute_change(bars_daily, fresh_today_bars)

    assert change == pytest.approx(235.0)
    assert change_pct == pytest.approx(235.0 / 2025 * 100)


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


def test_build_strategy_recommendations_lists_one_row_per_open_buy_signal(tmp_path):
    """NOTIFIABLE_STRATEGIES進場/出場都是edge-triggered，觸發後策略自己追蹤部位直到
    下一個相反方向事件——這裡看的是「每個策略最後一次動作是叫你買還是叫你賣」，不是
    像舊版buy_formula那種「條件現在還持續成立」的狀態。20天持平後單日創新高：
    atr_breakout(唐奇安突破)跟golden_cross_scaleout(打分制剛好5分：MA5>MA20+站上MA20+
    突破20日新高)都會進場，且之後沒有再出現賣出訊號——一列對應一個策略，兩列的「買進
    策略」各自填自己的策略名稱，「賣出策略」都留白。"""
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    config = make_config(db_path)

    closes = [50] * 20 + [60]
    dates = pd.date_range(end=pd.Timestamp.now().normalize(), periods=len(closes), freq="D")
    bars = [
        Bar(symbol="2454", ts=ts.to_pydatetime(), open=c, high=c + 1, low=c - 1, close=c, volume=1000)
        for ts, c in zip(dates, closes)
    ]

    with db.connect(db_path) as conn:
        db.insert_bars_daily(conn, bars)
        db.add_to_watchlist(conn, "2454", name="聯發科")

    rows = build_strategy_recommendations(config)

    assert len(rows) == 2
    buy_strategies = {r["買進策略"] for r in rows}
    assert buy_strategies == {"atr_breakout", "golden_cross_scaleout"}
    for row in rows:
        assert row["代號"] == "2454"
        assert row["名稱"] == "聯發科"
        assert row["現價"] == 60
        assert row["賣出策略"] == ""
        assert row["觸發價格"] == 60
        assert row["觸發日期"] == dates[-1].strftime("%Y-%m-%d")


def test_build_strategy_recommendations_uses_fresh_intraday_price_not_frozen_daily_snapshot(tmp_path):
    # 2026-08-13使用者發現：這張表的「現價」一直是直接讀bars_daily最後一筆，跟總覽表格
    # 修好的_current_price邏輯不一致——如果daily_update盤中抓到快照凍結在bars_daily，
    # 這裡也該像總覽表格一樣改用還在更新的bars_5min現價
    closes = [50] * 20 + [60]
    dates = pd.date_range(end=pd.Timestamp.now().normalize(), periods=len(closes), freq="D")
    bars = [
        Bar(symbol="2454", ts=ts.to_pydatetime(), open=c, high=c + 1, low=c - 1, close=c, volume=1000)
        for ts, c in zip(dates, closes)
    ]
    fresh_tick = Bar(
        symbol="2454",
        ts=(pd.Timestamp.now() - pd.Timedelta(minutes=3)).to_pydatetime(),
        open=70, high=75, low=65, close=70, volume=50,
    )

    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    config = make_config(db_path)

    with db.connect(db_path) as conn:
        db.insert_bars_daily(conn, bars)
        db.insert_bars_5min(conn, [fresh_tick])
        db.add_to_watchlist(conn, "2454", name="聯發科")

    rows = build_strategy_recommendations(config)

    assert all(r["現價"] == 70 for r in rows), "該用還在更新的bars_5min現價(70)，不是bars_daily最後一筆(60)"


def test_build_strategy_recommendations_keeps_both_buy_and_sell_rows_for_a_closed_position(tmp_path):
    # 先創新高進場，接著大跌觸發ATR停損賣出——2026-08-08使用者指出：買進事件不該被之後
    # 的賣出事件蓋掉，兩個都在100天內就該各自留一列，不然會跟「模擬交易紀錄」對不起來
    # (那邊看得到完整的一買一賣，這邊卻只看得到賣出，找不到對應的買進紀錄)。
    closes = [50] * 20 + [60, 40]
    dates = pd.date_range(end=pd.Timestamp.now().normalize(), periods=len(closes), freq="D")
    bars = [
        Bar(symbol="2454", ts=ts.to_pydatetime(), open=c, high=c + 1, low=c - 1, close=c, volume=1000)
        for ts, c in zip(dates, closes)
    ]
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    config = make_config(db_path)

    with db.connect(db_path) as conn:
        db.insert_bars_daily(conn, bars)
        db.add_to_watchlist(conn, "2454", name="聯發科")

    rows = build_strategy_recommendations(config)
    atr_rows = [r for r in rows if r["賣出策略"] == "atr_breakout" or r["買進策略"] == "atr_breakout"]

    assert len(atr_rows) == 2
    buy_row = next(r for r in atr_rows if r["買進策略"] == "atr_breakout")
    sell_row = next(r for r in atr_rows if r["賣出策略"] == "atr_breakout")
    assert buy_row["觸發價格"] == 60
    assert buy_row["觸發日期"] == dates[-2].strftime("%Y-%m-%d")
    assert sell_row["觸發價格"] == 40
    assert sell_row["觸發日期"] == dates[-1].strftime("%Y-%m-%d")
    assert buy_row["現價"] == sell_row["現價"] == 40


def test_build_strategy_recommendations_omits_signals_older_than_max_age(tmp_path):
    # 跟test_build_strategy_recommendations_lists_open_buy_signals_for_a_symbol同一組資料，
    # 只是把整段K棒往前搬到150天前(超過MAX_SIGNAL_AGE_DAYS=100天)——atr_breakout/
    # golden_cross_scaleout一樣會在最後一天觸發BUY，但因為太久沒動作了，不該再列出來，
    # 不管是買進還是賣出欄位都不行(這支股票整列都該被拿掉，因為沒有其他訊號)。
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    config = make_config(db_path)

    closes = [50] * 20 + [60]
    dates = pd.date_range(end=pd.Timestamp.now().normalize() - pd.Timedelta(days=150), periods=len(closes), freq="D")
    bars = [
        Bar(symbol="2454", ts=ts.to_pydatetime(), open=c, high=c + 1, low=c - 1, close=c, volume=1000)
        for ts, c in zip(dates, closes)
    ]

    with db.connect(db_path) as conn:
        db.insert_bars_daily(conn, bars)
        db.add_to_watchlist(conn, "2454", name="聯發科")

    assert build_strategy_recommendations(config) == []


def test_build_strategy_recommendations_excludes_symbol_with_no_signal_at_all(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    config = make_config(db_path)

    closes = [50] * 30  # 完全持平，策略都不會觸發任何進場/出場
    dates = pd.date_range("2026-01-02", periods=len(closes), freq="D")
    bars = [
        Bar(symbol="2454", ts=ts.to_pydatetime(), open=c, high=c, low=c, close=c, volume=1000)
        for ts, c in zip(dates, closes)
    ]

    with db.connect(db_path) as conn:
        db.insert_bars_daily(conn, bars)
        db.add_to_watchlist(conn, "2454", name="聯發科")

    assert build_strategy_recommendations(config) == []


def test_build_strategy_recommendations_skips_strategies_disabled_for_that_symbol(tmp_path):
    # 跟test_build_paper_trades_skips_strategies_disabled_for_that_symbol同一個bug：
    # 2026-08-08發現這裡漏了套用disabled_strategies，導致已經被排除(不會實際通知)的
    # 策略還出現在「建議買進」列表，使用者看了會誤以為這個策略還在運作。
    closes = [50] * 20 + [60]
    dates = pd.date_range(end=pd.Timestamp.now().normalize(), periods=len(closes), freq="D")
    bars = [
        Bar(symbol="2454", ts=ts.to_pydatetime(), open=c, high=c + 1, low=c - 1, close=c, volume=1000)
        for ts, c in zip(dates, closes)
    ]
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    config = make_config(db_path)

    with db.connect(db_path) as conn:
        db.insert_bars_daily(conn, bars)
        db.add_to_watchlist(conn, "2454", name="聯發科")
        db.set_disabled_strategies(conn, "2454", ["atr_breakout"])

    rows = build_strategy_recommendations(config)

    assert not any(r["買進策略"] == "atr_breakout" or r["賣出策略"] == "atr_breakout" for r in rows)
    assert any(r["買進策略"] == "golden_cross_scaleout" for r in rows), "沒被排除的策略應該照樣出現"


def test_build_paper_trades_lists_closed_round_trip_with_return_pct(tmp_path):
    # 創新高進場(60)接著大跌觸發ATR停損賣出(40)——一買一賣配成一筆已平倉交易，
    # 報酬率該是(40-60)/60*100 = -33.3%，不是隨便估的
    closes = [50] * 20 + [60, 40]
    dates = pd.date_range(end=pd.Timestamp.now().normalize(), periods=len(closes), freq="D")
    bars = [
        Bar(symbol="2454", ts=ts.to_pydatetime(), open=c, high=c + 1, low=c - 1, close=c, volume=1000)
        for ts, c in zip(dates, closes)
    ]
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    config = make_config(db_path)

    with db.connect(db_path) as conn:
        db.insert_bars_daily(conn, bars)
        db.add_to_watchlist(conn, "2454", name="聯發科")

    rows = build_paper_trades(config, start_date=dates[0].strftime("%Y-%m-%d"))

    atr_row = next(r for r in rows if r["策略"] == "atr_breakout")
    assert atr_row["狀態"] == "已平倉"
    assert atr_row["買進價位"] == 60
    assert atr_row["賣出價位"] == 40
    assert atr_row["報酬率(%)"] == pytest.approx(-33.3, abs=0.1)


def test_build_paper_trades_marks_open_position_as_held_with_unrealized_return(tmp_path):
    # 創新高進場(60)之後沒有再出現賣出訊號——還沒配到出場，該標記「持有中」，報酬率
    # 用現價(也是60，因為進場那天剛好是最後一天)估算，不該假裝已經平倉
    closes = [50] * 20 + [60]
    dates = pd.date_range(end=pd.Timestamp.now().normalize(), periods=len(closes), freq="D")
    bars = [
        Bar(symbol="2454", ts=ts.to_pydatetime(), open=c, high=c + 1, low=c - 1, close=c, volume=1000)
        for ts, c in zip(dates, closes)
    ]
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    config = make_config(db_path)

    with db.connect(db_path) as conn:
        db.insert_bars_daily(conn, bars)
        db.add_to_watchlist(conn, "2454", name="聯發科")

    rows = build_paper_trades(config, start_date=dates[0].strftime("%Y-%m-%d"))

    atr_row = next(r for r in rows if r["策略"] == "atr_breakout")
    assert atr_row["狀態"] == "持有中(未實現)"
    assert atr_row["買進價位"] == 60
    assert atr_row["賣出日期"] is None, "還沒真的賣，不該有賣出日期"
    assert atr_row["賣出價位"] is None, "還沒真的賣，不該有賣出價位"
    assert atr_row["現價"] == 60, "現價欄位獨立顯示，用來估算未實現報酬率"
    assert atr_row["報酬率(%)"] == 0


def test_build_paper_trades_ignores_signals_before_start_date(tmp_path):
    # start_date設在買進訊號那天之後——那筆交易的BUY事件被篩掉了，剩一個孤立的SELL
    # 配不成交易，也不該被誤判成部位，整筆不該出現
    closes = [50] * 20 + [60, 40]
    dates = pd.date_range(end=pd.Timestamp.now().normalize(), periods=len(closes), freq="D")
    bars = [
        Bar(symbol="2454", ts=ts.to_pydatetime(), open=c, high=c + 1, low=c - 1, close=c, volume=1000)
        for ts, c in zip(dates, closes)
    ]
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    config = make_config(db_path)

    with db.connect(db_path) as conn:
        db.insert_bars_daily(conn, bars)
        db.add_to_watchlist(conn, "2454", name="聯發科")

    rows = build_paper_trades(config, start_date=dates[-1].strftime("%Y-%m-%d"))

    assert not any(r["策略"] == "atr_breakout" for r in rows)


def test_build_paper_trades_skips_strategies_disabled_for_that_symbol(tmp_path):
    # 這裡跟_compute_track_records不一樣：模擬交易紀錄要反映「照現在的設定實際會不會
    # 被通知」，個股已經被disabled_strategies排除的策略不該出現在模擬交易裡，不然會
    # 看到「策略明明被排除了，畫面卻還在模擬買賣」這種矛盾
    closes = [50] * 20 + [60]
    dates = pd.date_range(end=pd.Timestamp.now().normalize(), periods=len(closes), freq="D")
    bars = [
        Bar(symbol="2454", ts=ts.to_pydatetime(), open=c, high=c + 1, low=c - 1, close=c, volume=1000)
        for ts, c in zip(dates, closes)
    ]
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    config = make_config(db_path)

    with db.connect(db_path) as conn:
        db.insert_bars_daily(conn, bars)
        db.add_to_watchlist(conn, "2454", name="聯發科")
        db.set_disabled_strategies(conn, "2454", ["atr_breakout"])

    rows = build_paper_trades(config, start_date=dates[0].strftime("%Y-%m-%d"))

    assert not any(r["策略"] == "atr_breakout" for r in rows)
    assert any(r["策略"] == "golden_cross_scaleout" for r in rows), "沒被排除的策略應該照樣出現"


def test_price_text_plain_number_when_change_unknown():
    assert price_text(105.0, None) == "105"


def test_price_text_passes_through_none():
    assert price_text(None, 2.0) == "—"
