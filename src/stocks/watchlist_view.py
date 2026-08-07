"""觀察清單總覽表格要顯示什麼、怎麼算，跟「怎麼畫UI」分開。dashboard/app.py只呼叫
build_overview_rows()把結果丟進st.dataframe，不自己算任何指標。"""
from datetime import datetime

import pandas as pd

from stocks.config import Config
from stocks.db import (
    attach_institutional_flows,
    bars_to_dataframe,
    connect,
    fetch_bars_5min_today,
    fetch_bars_daily,
    fetch_institutional_flows,
    fetch_watchlist,
    get_disabled_strategies,
)
from stocks.indicators import bollinger_bands, macd, rolling_avg_volume, rsi, sma, stochastic_kd
from stocks.models import Direction
from stocks.notifier import NOTIFIABLE_STRATEGIES
from stocks.strategies import STRATEGY_REGISTRY
from stocks.strategy_selection import SCALEOUT_STRATEGY
from stocks.strategy_stats import simulate_round_trips, simulate_scaleout_trades

MAX_SIGNAL_AGE_DAYS = 100  # 超過這個天數的舊訊號直接不列(不是變灰/變淡)——那個策略對這支
# 股票已經一段時間沒有任何動作，不管上次是買還是賣都不算「現在還有意義的訊號」，2026-08-07
# 使用者確認拿掉，不是全部保留只是換顏色

MA_PERIODS = (5, 10, 20, 60)
MA_NAMES = {20: "月", 60: "季"}  # 5、10維持數字講法，20/60改叫月線/季線
KD_CHART_LOOKBACK_DAYS = 20  # KD是看線圖找交叉，不是看單一數字，只留最近一段給小圖用
LIMIT_THRESHOLD_PCT = 9.5  # 台股漲跌停是±10%，但實際限制價會依股價檔位四捨五入(通常落在9.7~10.0%)，
# 抓9.5%當「接近/觸及漲跌停」的門檻，不算精確的檔位換算


def _round_or_none(value, ndigits=1):
    """四捨五入到ndigits位，小數點後全是0的話回傳整數（顯示不會多一個「.0」）。"""
    if value is None or pd.isna(value):
        return None
    rounded = round(float(value), ndigits)
    return int(rounded) if rounded == int(rounded) else rounded


def _rsi_text(value, oversold=30, overbought=70) -> str:
    """超賣(可能反彈)標紅、超買(可能回落)標綠，中性不特別上色——顏色門檻跟標籤門檻共用
    oversold/overbought，不是另外訂50中線，兩者才會一致。"""
    if value is None or pd.isna(value):
        return "—"
    if value < oversold:
        label, color = "超賣", "red"
    elif value > overbought:
        label, color = "超買", "green"
    else:
        label, color = "中性", "inherit"
    return f'<span style="color:{color}">{label} ({value:.0f})</span>'


def _macd_text(histogram) -> str:
    """紅漲綠跌：多頭紅色、空頭綠色。"""
    if histogram is None or pd.isna(histogram):
        return "—"
    label = "多頭" if histogram > 0 else "空頭" if histogram < 0 else "中性"
    color = "red" if histogram > 0 else "green" if histogram < 0 else "inherit"
    sign = "+" if histogram >= 0 else ""
    return f'<span style="color:{color}">{label} ({sign}{histogram:.1f})</span>'


def _bollinger_text(close, upper, lower) -> str:
    if close is None or pd.isna(upper) or pd.isna(lower) or upper == lower:
        return "—"
    pct_b = (close - lower) / (upper - lower)
    if pct_b >= 0.8:
        return "接近上軌"
    if pct_b <= 0.2:
        return "接近下軌"
    return "中間"


def _volume_text(ratio, multiplier=2) -> str:
    if ratio is None or pd.isna(ratio):
        return "—"
    label = "爆量" if ratio >= multiplier else "正常"
    return f"{label} ({ratio:.1f}倍)"


def _current_streak(series: pd.Series) -> tuple:
    """回傳(sign, length)：從最新一天往前數，同方向(買超/賣超)連續幾天。NaN視為中斷。"""
    clean = series.dropna()
    if clean.empty:
        return 0, 0
    values = clean.to_numpy()
    last_sign = 1 if values[-1] > 0 else (-1 if values[-1] < 0 else 0)
    if last_sign == 0:
        return 0, 0
    length = 0
    for v in reversed(values):
        sign = 1 if v > 0 else (-1 if v < 0 else 0)
        if sign != last_sign:
            break
        length += 1
    return last_sign, length


def institutional_text(foreign_net: pd.Series, trust_net: pd.Series, streak_threshold: int = 3) -> str:
    """連買/連賣天數達streak_threshold(預設3天)才標紅(連買)/綠(連賣)提醒，
    未達門檻的短天數streak維持一般文字顏色，不用整段都上色。"""
    parts = []
    for series, label in [(foreign_net, "外資"), (trust_net, "投信")]:
        sign, length = _current_streak(series)
        if length == 0:
            continue
        verb = "連買" if sign == 1 else "連賣"
        text = f"{label}：{verb}{length}日"
        if length >= streak_threshold:
            color = "red" if sign == 1 else "green"
            text = f'<span style="color:{color}">{text}</span>'
        parts.append(text)
    return "\n".join(parts) if parts else "—"


def _prev_close(bars_daily_df: pd.DataFrame):
    """今天以前最後一個收盤價 -- 「漲跌」的比較基準，跟市場報價慣例一致，不是當天開盤價。"""
    if bars_daily_df.empty:
        return None
    today = pd.Timestamp.now().normalize()
    before_today = bars_daily_df[bars_daily_df.index < today]
    if before_today.empty:
        return None
    return before_today["close"].iloc[-1]


def compute_change(bars_daily_df: pd.DataFrame, today_bars_df: pd.DataFrame):
    """回傳(change, change_pct)：現價比較昨收的漲跌點數/百分比。優先用bars_daily當天那筆
    (每天固定更新，最後、最完整的數字)；只有今天的日K還沒進來時才退回today_bars_df
    (run_live.py即時累積的盤中5分K)——這張表只在run_live.py確實掛著的時候才會有資料，
    一旦程式停掉，裡面最後一筆會凍結在停掉的那個時間點，不能無條件當作「現價」。"""
    prev_close = _prev_close(bars_daily_df)
    if prev_close is None:
        return None, None

    today = pd.Timestamp.now().normalize()
    today_daily_row = bars_daily_df[bars_daily_df.index >= today]
    if not today_daily_row.empty:
        current = today_daily_row["close"].iloc[-1]
    elif not today_bars_df.empty:
        current = today_bars_df["close"].iloc[-1]
    else:
        current = bars_daily_df["close"].iloc[-1]
    change = current - prev_close
    change_pct = (change / prev_close * 100) if prev_close else None
    return change, change_pct


def change_text(change, change_pct) -> str:
    """紅漲綠跌(台股慣例)，回傳的是帶顏色的HTML，呼叫端要用st.markdown(unsafe_allow_html=True)渲染。"""
    if change is None or pd.isna(change) or change_pct is None or pd.isna(change_pct):
        return "—"
    color = "red" if change > 0 else "green" if change < 0 else "inherit"
    sign = "+" if change >= 0 else ""
    return f'<span style="color:{color}">{sign}{change:.1f} ({sign}{change_pct:.1f}%)</span>'


def price_text(latest_close, change_pct) -> str:
    """目前價位紅漲綠跌上色；漲停/跌停時整格底色亮起來(仿券商APP)，回傳帶顏色的HTML，
    呼叫端要用st.markdown(unsafe_allow_html=True)渲染。"""
    price = _round_or_none(latest_close)
    if price is None:
        return "—"
    if change_pct is None or pd.isna(change_pct):
        return str(price)
    if change_pct >= LIMIT_THRESHOLD_PCT:
        return f'<span style="background-color:red;color:white;padding:1px 6px;border-radius:3px;">{price}</span>'
    if change_pct <= -LIMIT_THRESHOLD_PCT:
        return f'<span style="background-color:green;color:white;padding:1px 6px;border-radius:3px;">{price}</span>'
    color = "red" if change_pct > 0 else "green" if change_pct < 0 else "inherit"
    return f'<span style="color:{color}">{price}</span>'


def _ma_price_text(latest_close, ma_series: pd.Series) -> str:
    """5日/10日/月線/季線這幾欄：數字顏色代表「現價站上/跌破均線」(站上=紅/多方，跌破=
    綠/空方)；箭頭代表「均線本身比昨天上揚/下彎」，跟數字顏色是兩個獨立的維度(均線可能
    上揚但現價還沒站上，或現價站上但均線本身還在下彎)，所以箭頭用自己的紅漲/綠跌上色
    (跟大盤慣例一致)，不跟著數字顏色走，兩者可能同時顯示不同色。"""
    ma_value = ma_series.iloc[-1]
    value = _round_or_none(ma_value)
    if value is None:
        return "—"
    color = "red" if latest_close > ma_value else "green" if latest_close < ma_value else "inherit"

    arrow = ""
    if len(ma_series) >= 2:
        prev_ma = ma_series.iloc[-2]
        if not pd.isna(prev_ma):
            if ma_value > prev_ma:
                arrow = ' <span style="color:red">↑</span>'
            elif ma_value < prev_ma:
                arrow = ' <span style="color:green">↓</span>'

    return f'<span style="color:{color}">{value}</span>{arrow}'


def _empty_row(symbol: str, name: str) -> dict:
    row = {
        "代號": symbol,
        "名稱": name or "—",
        "漲跌": "—",
        "目前價位": "—",
        "昨收": None,
        "KD": pd.DataFrame(columns=["k", "d"]),
    }
    for label in ["5日", "10日", "月線", "季線", "RSI", "MACD", "布林通道", "成交量", "三大法人"]:
        row[label] = "—"
    return row


def build_overview_rows(config: Config) -> list[dict]:
    rsi_params = config.strategy_params.get("rsi", {})
    macd_params = config.strategy_params.get("macd", {})
    bollinger_params = config.strategy_params.get("bollinger", {})
    volume_params = config.strategy_params.get("volume_anomaly", {})
    kd_params = config.strategy_params.get("kd", {})

    rows = []
    with connect(config.db_path) as conn:
        for symbol_row in fetch_watchlist(conn):
            symbol, name = symbol_row["code"], symbol_row["name"]
            bars = bars_to_dataframe(fetch_bars_daily(conn, symbol), ts_field="date")
            if bars.empty:
                rows.append(_empty_row(symbol, name))
                continue

            flows = fetch_institutional_flows(conn, symbol)
            merged = attach_institutional_flows(bars, flows)
            today_bars = bars_to_dataframe(fetch_bars_5min_today(conn, symbol), ts_field="ts")
            change, change_pct = compute_change(bars, today_bars)
            prev_close = _prev_close(bars)

            close = bars["close"]
            latest_close = close.iloc[-1]
            ma_series = {p: sma(close, p) for p in MA_PERIODS}

            rsi_value = rsi(close, rsi_params.get("period", 14)).iloc[-1]
            _, _, histogram = macd(close, macd_params.get("fast", 12), macd_params.get("slow", 26), macd_params.get("signal", 9))
            upper, _, lower = bollinger_bands(close, bollinger_params.get("period", 20), bollinger_params.get("num_std", 2))
            avg_volume = rolling_avg_volume(bars["volume"], volume_params.get("avg_period", 20)).iloc[-1]
            volume_ratio = bars["volume"].iloc[-1] / avg_volume if avg_volume else None
            k, d = stochastic_kd(
                bars["high"], bars["low"], close,
                kd_params.get("rsv_period", 9), kd_params.get("k_smooth", 3), kd_params.get("d_smooth", 3),
            )

            rows.append(
                {
                    "代號": symbol,
                    "名稱": name or "—",
                    "漲跌": change_text(change, change_pct),
                    "目前價位": price_text(latest_close, change_pct),
                    "昨收": prev_close,
                    "5日": _ma_price_text(latest_close, ma_series[5]),
                    "10日": _ma_price_text(latest_close, ma_series[10]),
                    "月線": _ma_price_text(latest_close, ma_series[20]),
                    "季線": _ma_price_text(latest_close, ma_series[60]),
                    "RSI": _rsi_text(rsi_value, rsi_params.get("oversold", 30), rsi_params.get("overbought", 70)),
                    "MACD": _macd_text(histogram.iloc[-1]),
                    "布林通道": _bollinger_text(latest_close, upper.iloc[-1], lower.iloc[-1]),
                    "成交量": _volume_text(volume_ratio, volume_params.get("multiplier", 2)),
                    "KD": pd.DataFrame({"k": k, "d": d}).tail(KD_CHART_LOOKBACK_DAYS),
                    "三大法人": institutional_text(
                        merged["foreign_net"] if "foreign_net" in merged.columns else pd.Series(dtype=float),
                        merged["trust_net"] if "trust_net" in merged.columns else pd.Series(dtype=float),
                    ),
                }
            )

    return rows


def build_strategy_recommendations(config: Config) -> list[dict]:
    """觀察清單裡每支股票、每個策略目前最後一個事件——不管方向是BUY還是SELL都列出來，
    一列對應一個策略的訊號(不是把整支股票的好幾個策略塞進同一格文字)。NOTIFIABLE_STRATEGIES
    這幾個策略進場/出場都是edge-triggered(條件第一天成立才發一次)，觸發後策略自己會追蹤
    部位直到下一個相反方向事件，不像舊版buy_formula有「持續符合就一直列著」的連續狀態
    可以看。這裡看的是「這個策略最後一次動作是叫你買還是叫你賣」，不是「條件現在還成立」。
    每一列的「買進策略」跟「賣出策略」剛好一個有填一個留白(同一個策略同一時間不會又是
    買又是賣)：最後一次是BUY就填「買進策略」，是SELL就填「賣出策略」。「觸發價格」是
    那個事件當天的收盤價(那個策略真正判斷買/賣的依據)，「現價」是今天的收盤價，兩個
    分開放才看得出來「當時觸發之後，現在漲跌多少」。超過MAX_SIGNAL_AGE_DAYS(100天)沒
    動作的策略直接不列——2026-08-07發現沒有天數限制的話，會把一年多前的舊訊號跟這幾天
    的新訊號混在一起，使用者確認拿掉超過100天沒動作的，不是保留但改樣式。"""
    rows = []
    with connect(config.db_path) as conn:
        for symbol_row in fetch_watchlist(conn):
            symbol, name = symbol_row["code"], symbol_row["name"]
            bars = bars_to_dataframe(fetch_bars_daily(conn, symbol), ts_field="date")
            if bars.empty:
                continue

            flows = fetch_institutional_flows(conn, symbol)
            merged = attach_institutional_flows(bars, flows)
            current_price = _round_or_none(bars["close"].iloc[-1])

            for strategy_name in NOTIFIABLE_STRATEGIES:
                strategy = STRATEGY_REGISTRY.get(strategy_name)
                if strategy is None:
                    continue
                events = strategy.evaluate(symbol, merged, config.strategy_params.get(strategy_name, {}))
                if not events:
                    continue
                last_event = max(events, key=lambda e: e.ts)
                if (datetime.now() - last_event.ts).days > MAX_SIGNAL_AGE_DAYS:
                    continue  # 這個策略對這支股票太久沒動作了，不算現在有意義的訊號

                is_buy = last_event.direction == Direction.BUY
                rows.append(
                    {
                        "代號": symbol,
                        "名稱": name or "—",
                        "買進策略": strategy_name if is_buy else "",
                        "賣出策略": strategy_name if not is_buy else "",
                        "觸發價格": _round_or_none(last_event.price),
                        "現價": current_price,
                        "觸發日期": last_event.ts.strftime("%Y-%m-%d"),
                    }
                )

    return rows


def build_paper_trades(config: Config, start_date: str = "2026-07-01") -> list[dict]:
    """模擬交易紀錄：從start_date開始，NOTIFIABLE_STRATEGIES每個策略每次BUY訊號就當作
    買進、配對到下一個SELL訊號(或golden_cross_scaleout兩次SELL)就當作賣出，純粹照著
    訊號模擬記錄買賣價位跟報酬率，不是真的下單——給使用者觀察這幾個策略實際表現用。
    start_date當天之前已經在場內的部位不算(從那天開始當作空手重新起算，跟
    simulate_round_trips/simulate_scaleout_trades本來的配對邏輯一致：先篩選事件範圍
    再配對)。還沒配到出場的部位標記「持有中」，「賣出價位」留空(還沒真的賣)，另外用
    「現價」欄位算未實現報酬率——兩者分開列，不能讓「持有中」那列的賣出價位看起來
    像已經賣掉了。

    這裡跟_compute_track_records不一樣：那裡是故意忽略排除清單(給使用者看「為什麼」
    被排除的歷史全貌)，這裡是模擬「照現在的設定實際會不會被通知」，所以個股已經被
    disabled_strategies排除的策略要跳過，不列進模擬交易——不然會看到「策略明明已經
    被排除了，畫面上卻還在模擬買賣」這種矛盾。"""
    start = pd.Timestamp(start_date)
    rows = []
    with connect(config.db_path) as conn:
        for symbol_row in fetch_watchlist(conn):
            symbol, name = symbol_row["code"], symbol_row["name"]
            bars = bars_to_dataframe(fetch_bars_daily(conn, symbol), ts_field="date")
            if bars.empty:
                continue

            flows = fetch_institutional_flows(conn, symbol)
            merged = attach_institutional_flows(bars, flows)
            current_price = bars["close"].iloc[-1]
            disabled = set(get_disabled_strategies(conn, symbol))

            for strategy_name in NOTIFIABLE_STRATEGIES:
                if strategy_name in disabled:
                    continue
                strategy = STRATEGY_REGISTRY.get(strategy_name)
                if strategy is None:
                    continue
                events = strategy.evaluate(symbol, merged, config.strategy_params.get(strategy_name, {}))
                events_since = [e for e in events if e.ts >= start]
                if not events_since:
                    continue

                base_row = {"代號": symbol, "名稱": name or "—", "策略": strategy_name}

                if strategy_name == SCALEOUT_STRATEGY:
                    trades, still_open = simulate_scaleout_trades(events_since)
                    for t in trades:
                        rows.append(
                            {
                                **base_row,
                                "買進日期": t.entry_ts.strftime("%Y-%m-%d"),
                                "買進價位": _round_or_none(t.entry_price),
                                "賣出日期": t.exit2_ts.strftime("%Y-%m-%d"),
                                "賣出價位": _round_or_none(t.blended_exit_price),
                                "現價": _round_or_none(current_price),
                                "報酬率(%)": _round_or_none(t.return_pct),
                                "狀態": "已平倉",
                            }
                        )
                    if still_open:
                        entry, exits = still_open["entry"], still_open["exits"]
                        unrealized_price = (exits[0].price + current_price) / 2 if exits else current_price
                        rows.append(
                            {
                                **base_row,
                                "買進日期": entry.ts.strftime("%Y-%m-%d"),
                                "買進價位": _round_or_none(entry.price),
                                "賣出日期": None,
                                "賣出價位": None,
                                "現價": _round_or_none(current_price),
                                "報酬率(%)": _round_or_none((unrealized_price - entry.price) / entry.price * 100),
                                "狀態": "持有中(未實現)",
                            }
                        )
                else:
                    trades, open_position = simulate_round_trips(events_since)
                    for t in trades:
                        rows.append(
                            {
                                **base_row,
                                "買進日期": t.entry_ts.strftime("%Y-%m-%d"),
                                "買進價位": _round_or_none(t.entry_price),
                                "賣出日期": t.exit_ts.strftime("%Y-%m-%d"),
                                "賣出價位": _round_or_none(t.exit_price),
                                "現價": _round_or_none(current_price),
                                "報酬率(%)": _round_or_none(t.return_pct),
                                "狀態": "已平倉",
                            }
                        )
                    if open_position:
                        rows.append(
                            {
                                **base_row,
                                "買進日期": open_position.ts.strftime("%Y-%m-%d"),
                                "買進價位": _round_or_none(open_position.price),
                                "賣出日期": None,
                                "賣出價位": None,
                                "現價": _round_or_none(current_price),
                                "報酬率(%)": _round_or_none(
                                    (current_price - open_position.price) / open_position.price * 100
                                ),
                                "狀態": "持有中(未實現)",
                            }
                        )

    return rows
