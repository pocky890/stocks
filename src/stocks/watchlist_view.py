"""觀察清單總覽表格要顯示什麼、怎麼算，跟「怎麼畫UI」分開。dashboard/app.py只呼叫
build_overview_rows()把結果丟進st.dataframe，不自己算任何指標。"""
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
)
from stocks.indicators import bollinger_bands, macd, rolling_avg_volume, rsi, sma, stochastic_kd
from stocks.strategies.composite_formula import compute_buy_condition

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
    """回傳(change, change_pct)：現價(今天盤中最新一筆，沒有就用日線最新收盤)比較昨收的漲跌點數/百分比。"""
    prev_close = _prev_close(bars_daily_df)
    if prev_close is None:
        return None, None

    current = today_bars_df["close"].iloc[-1] if not today_bars_df.empty else bars_daily_df["close"].iloc[-1]
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
    """5日/10日/月線/季線這幾欄：現價站上均線=紅色(多方)，跌破均線=綠色(空方)；後面加↑/↓
    表示均線本身正在上揚/下彎——跟現價站上/跌破是兩件不同的事(均線可能上揚但現價還沒
    站上，或現價站上但均線本身還在下彎)，箭頭比較的是均線今天跟昨天的值，不是價格。"""
    ma_value = ma_series.iloc[-1]
    value = _round_or_none(ma_value)
    if value is None:
        return "—"
    color = "red" if latest_close > ma_value else "green" if latest_close < ma_value else "inherit"

    arrow = ""
    if len(ma_series) >= 2:
        prev_ma = ma_series.iloc[-2]
        if not pd.isna(prev_ma):
            arrow = " ↑" if ma_value > prev_ma else " ↓" if ma_value < prev_ma else ""

    return f'<span style="color:{color}">{value}{arrow}</span>'


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


def _current_true_streak_start(condition: pd.Series):
    """從最後一天往前數，condition連續為True的第一天是哪一天(從什麼時候開始「一直符合到
    現在」)。condition是空的或最後一天是False就回傳None。"""
    if condition.empty or not bool(condition.iloc[-1]):
        return None
    idx = len(condition) - 1
    while idx > 0 and bool(condition.iloc[idx - 1]):
        idx -= 1
    return condition.index[idx]


def build_buy_recommendations(config: Config) -> list[dict]:
    """觀察清單裡「極簡買進公式」3步驟目前還成立的股票——跟build_overview_rows不一樣，
    這裡看的是「現在還符合嗎」(狀態)，不是「今天剛觸發」(edge)。edge-triggered的通知
    只在條件第一次成立那天發一次，如果訊號出現時你還沒來得及進場(考慮兩天、等資金)，
    通知早就過去了，但這裡只要條件還沒被打破，就會一直列在清單裡，不會因為錯過那一天
    的通知就整個看不到。"""
    params = config.strategy_params.get("buy_formula", {})
    rows = []
    with connect(config.db_path) as conn:
        for symbol_row in fetch_watchlist(conn):
            symbol, name = symbol_row["code"], symbol_row["name"]
            bars = bars_to_dataframe(fetch_bars_daily(conn, symbol), ts_field="date")
            if bars.empty:
                continue

            flows = fetch_institutional_flows(conn, symbol)
            merged = attach_institutional_flows(bars, flows)
            condition, _ = compute_buy_condition(merged, params)
            since = _current_true_streak_start(condition)
            if since is None:
                continue

            rows.append(
                {
                    "代號": symbol,
                    "名稱": name or "—",
                    "現價": _round_or_none(bars["close"].iloc[-1]),
                    "符合日期": since.strftime("%Y-%m-%d"),
                }
            )

    return rows
