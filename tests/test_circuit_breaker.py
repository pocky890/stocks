from datetime import date, timedelta

import pandas as pd

from stocks import db
from stocks.circuit_breaker import (
    compute_breadth_pct,
    compute_breadth_series,
    is_buy_suppressed,
    load_active_state,
    refresh_industry_states,
    replay_active_state,
)
from stocks.config import Config


def make_config(db_path: str, enter=0.60, exit_=0.40, ma_period=3) -> Config:
    return Config(
        shioaji_api_key="",
        shioaji_secret_key="",
        telegram_bot_token="",
        telegram_chat_id="",
        market_open="09:00",
        market_close="13:30",
        bar_interval_minutes=5,
        batch_pacing_seconds=0,
        strategy_params={},
        db_path=db_path,
        circuit_breaker_ma_period=ma_period,
        circuit_breaker_enter_threshold=enter,
        circuit_breaker_exit_threshold=exit_,
    )


def insert_closes(conn, industry_code: str, symbol: str, closes: list, start: date) -> None:
    rows = [
        {"symbol": symbol, "date": (start + timedelta(days=i)).isoformat(), "industry_code": industry_code, "close": c}
        for i, c in enumerate(closes)
    ]
    db.insert_industry_closes(conn, rows)


def test_compute_breadth_pct_returns_none_when_not_enough_history(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        insert_closes(conn, "24", "AAA", [10, 11], date(2026, 1, 1))  # only 2 days, ma_period=3
        pct = compute_breadth_pct(conn, "24", ma_period=3)
    assert pct is None


def test_compute_breadth_pct_counts_symbols_below_their_own_ma(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    start = date(2026, 1, 1)
    with db.connect(db_path) as conn:
        # AAA: rising -> last close (12) above its own 3-day MA (10) -> not below
        insert_closes(conn, "24", "AAA", [8, 10, 12], start)
        # BBB: falling -> last close (8) below its own 3-day MA (10) -> below
        insert_closes(conn, "24", "BBB", [12, 10, 8], start)
        # CCC: falling -> also below
        insert_closes(conn, "24", "CCC", [12, 10, 8], start)
        pct = compute_breadth_pct(conn, "24", ma_period=3)
    assert pct == 2 / 3


def test_refresh_industry_states_has_hysteresis(tmp_path):
    """Enters at >=enter_threshold, stays active until <=exit_threshold -- a reading in
    between the two thresholds must not flip an already-active industry back off."""
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    config = make_config(db_path, enter=0.60, exit_=0.40, ma_period=3)
    start = date(2026, 1, 1)

    with db.connect(db_path) as conn:
        # refresh_industry_states only refreshes codes it finds in symbols.industry_code
        # (populated by scripts/populate_industry_codes.py in production) -- not just
        # whatever happens to already be in industry_closes.
        db.upsert_industry_universe(conn, [{"code": "AAA", "name": "AAA", "market": "TWSE", "industry_code": "24"}])
        # Day 1 data: all 4 symbols below their own MA -> 100% breadth, well above enter_threshold
        for sym in ("AAA", "BBB", "CCC", "DDD"):
            insert_closes(conn, "24", sym, [12, 10, 8], start)
        state = refresh_industry_states(conn, config)
    assert state["24"] is True

    with db.connect(db_path) as conn:
        # Extend history: 2 of 4 recover above their own MA, 2 stay below -> exactly 50%,
        # squarely between exit(40%) and enter(60%) -- must stay ON, not flip off.
        insert_closes(conn, "24", "AAA", [8, 9, 10, 11], start)
        insert_closes(conn, "24", "BBB", [8, 9, 10, 11], start)
        insert_closes(conn, "24", "CCC", [12, 10, 8, 6], start)
        insert_closes(conn, "24", "DDD", [12, 10, 8, 6], start)
        state = refresh_industry_states(conn, config)
    assert state["24"] is True, "50% breadth is between exit(40%) and enter(60%) -- must stay ON"

    with db.connect(db_path) as conn:
        # Now everyone recovers above their own MA (0% breadth) -> should turn off.
        insert_closes(conn, "24", "AAA", [8, 9, 10, 11, 12, 13], start)
        insert_closes(conn, "24", "BBB", [8, 9, 10, 11, 12, 13], start)
        insert_closes(conn, "24", "CCC", [12, 10, 8, 6, 12, 13], start)
        insert_closes(conn, "24", "DDD", [12, 10, 8, 6, 12, 13], start)
        state = refresh_industry_states(conn, config)
    assert state["24"] is False


def test_is_buy_suppressed_requires_both_industry_active_and_own_ma_broken():
    industry_codes = {"3711": "24"}
    bars_below_own_ma = pd.DataFrame({"close": [12, 10, 8]})
    bars_above_own_ma = pd.DataFrame({"close": [8, 10, 12]})

    # Industry active, but this stock itself hasn't broken its own MA -- must not suppress
    # (the 3711 false-positive case this feature was built to fix).
    assert is_buy_suppressed("3711", industry_codes, {"24": True}, bars_above_own_ma, own_ma_period=3) is False

    # Industry active AND stock itself below its own MA -- suppress.
    assert is_buy_suppressed("3711", industry_codes, {"24": True}, bars_below_own_ma, own_ma_period=3) is True

    # Industry not active -- never suppress regardless of the stock's own trend.
    assert is_buy_suppressed("3711", industry_codes, {"24": False}, bars_below_own_ma, own_ma_period=3) is False


def test_is_buy_suppressed_never_blocks_unclassified_symbol():
    assert is_buy_suppressed("9999", {}, {"24": True}, pd.DataFrame({"close": [12, 10, 8]}), own_ma_period=3) is False


def test_is_buy_suppressed_with_own_ma_period_none_ignores_own_trend():
    # 2026-08-16使用者確認：own_ma_period=None(現行預設)拿掉「自己也跌破均線」這道AND
    # 條件，純看產業寬度是否觸發——golden_cross/trend_following/long_swing/
    # atr_breakout/breakout這幾支策略的進場條件本身就要求「站上」某條均線，跟這道AND
    # 條件幾乎互斥，導致實測擋下率是0%(見scripts/backtest_circuit_breaker_own_ma.py)。
    industry_codes = {"3711": "24"}
    bars_above_own_ma = pd.DataFrame({"close": [8, 10, 12]})

    # 產業寬度沒觸發：即使own_ma_period=None，一樣不擋。
    assert is_buy_suppressed("3711", industry_codes, {"24": False}, bars_above_own_ma, own_ma_period=None) is False

    # 產業寬度觸發，own_ma_period=None：不管這支股票自己站不站得上均線都擋——這是拿掉
    # AND條件後的代價(3711那種逆勢股會被誤擋)，是使用者看過具體案例後接受的取捨。
    assert is_buy_suppressed("3711", industry_codes, {"24": True}, bars_above_own_ma, own_ma_period=None) is True


def test_compute_breadth_series_returns_pct_for_every_day_not_just_latest(tmp_path):
    # build_paper_trades要回放「當時」的斷路器狀態，不能只看最新一天(compute_breadth_pct
    # 的行為)，所以每一天都要各自算一個寬度數字，不夠ma_period天的日子直接不出現。
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    start = date(2026, 1, 1)
    with db.connect(db_path) as conn:
        insert_closes(conn, "24", "AAA", [8, 10, 12, 14], start)  # 一路走高 -> 每天都在自己均線之上
        insert_closes(conn, "24", "BBB", [12, 10, 8, 6], start)  # 一路走低 -> 每天都在自己均線之下
        series = compute_breadth_series(conn, "24", ma_period=3)
    assert list(series.index.strftime("%Y-%m-%d")) == ["2026-01-03", "2026-01-04"]
    assert list(series.values) == [0.5, 0.5]


def test_replay_active_state_applies_same_hysteresis_day_by_day():
    # 跟refresh_industry_states同一套60%/40%遲滯規則，但這裡是一次回放整段歷史(給paper
    # trade用)，不是只看單一個「現在」的百分比。
    pct = pd.Series(
        [0.9, 0.5, 0.9, 0.3, 0.9],
        index=pd.date_range("2026-01-01", periods=5),
    )
    state = replay_active_state(pct, enter_threshold=0.60, exit_threshold=0.40)
    assert list(state.values) == [True, True, True, False, True]


def test_load_active_state_defaults_to_empty_dict(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        assert load_active_state(conn) == {}
