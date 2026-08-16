"""觀察清單總覽表格要顯示什麼、怎麼算，跟「怎麼畫UI」分開。dashboard/app.py只呼叫
build_overview_rows()把結果丟進st.dataframe，不自己算任何指標。"""
from datetime import datetime

import pandas as pd

from stocks.circuit_breaker import CIRCUIT_BREAKER_EXEMPT_STRATEGIES, compute_breadth_series, replay_active_state
from stocks.config import Config
from stocks.db import (
    attach_institutional_flows,
    attach_monthly_revenue_growth,
    bars_to_dataframe,
    connect,
    fetch_all_industry_codes,
    fetch_bars_5min_latest_day,
    fetch_bars_daily,
    fetch_institutional_flows,
    fetch_monthly_revenue,
    fetch_watchlist,
    get_disabled_strategies,
)
from stocks.indicators import bollinger_bands, macd, rolling_avg_volume, rsi, sma, stochastic_kd
from stocks.models import Direction
from stocks.notifier import NOTIFIABLE_STRATEGIES
from stocks.strategies import STRATEGY_REGISTRY
from stocks.strategy_stats import is_scaleout_strategy, simulate_round_trips, simulate_scaleout_trades

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


INTRADAY_FRESH_WITHIN = pd.Timedelta(minutes=15)  # bars_5min超過這個時間沒有新資料就視為
# 停止更新(run_live.py斷線/沒開)，不能再信任它是「現價」；15分鐘=3根5分K的寬容度，避開
# 單次盤中小斷線就整欄顯示壞掉。


def _reference_date(bars_daily_df: pd.DataFrame, today_bars_df: pd.DataFrame) -> pd.Timestamp:
    """決定_current_price實際採用的是哪一天的資料——判斷邏輯必須跟_current_price完全
    對齊，因為_prev_close要拿「這一天以前」當基準。2026-08-17發現的bug：原本_prev_close
    直接用pd.Timestamp.now()的日曆日期切「以前」，非交易日(週末/國定假日)當天bars_daily
    根本沒有那一列，_current_price會退回歷史最後一筆(通常是上一個交易日，例如週五)當
    現價，但_prev_close卻還是用「日曆上的今天」去切，週五那一列剛好還是被算進「今天以前」，
    導致current跟prev_close指向同一天，漲跌恆為0(使用者在非交易日看到的樣子)。"""
    now = pd.Timestamp.now()
    if not today_bars_df.empty and (now - today_bars_df.index[-1]) <= INTRADAY_FRESH_WITHIN:
        return now.normalize()

    today = now.normalize()
    today_daily_row = bars_daily_df[bars_daily_df.index >= today]
    if not today_daily_row.empty:
        return today
    if not today_bars_df.empty:
        return now.normalize()
    if bars_daily_df.empty:
        return today
    return bars_daily_df.index[-1].normalize()  # 非交易日：退回歷史最後一筆代表的那一天


def _prev_close(bars_daily_df: pd.DataFrame, reference_date: pd.Timestamp):
    """reference_date以前最後一個收盤價 -- 「漲跌」的比較基準，跟市場報價慣例一致，
    不是當天開盤價。reference_date一定要跟_current_price實際採用的那一天一致(見
    _reference_date)，不能直接用日曆上的今天，否則非交易日會讓兩者意外指向同一天。"""
    if bars_daily_df.empty:
        return None
    before_reference = bars_daily_df[bars_daily_df.index < reference_date]
    if before_reference.empty:
        return None
    return before_reference["close"].iloc[-1]


def _current_price(bars_daily_df: pd.DataFrame, today_bars_df: pd.DataFrame):
    """現價的單一計算依據，「漲跌」跟「目前價位」/均線比較/RSI/MACD/布林通道都要用
    同一個數字算，不能各自算出不同的現價看起來自相矛盾。

    2026-08-13發現：不能只看「bars_daily有沒有今天這一列」就直接信任它——daily_update.py
    的每日檢查(check_and_update)一天只跑一次，但這一次可能發生在盤中(使用者當天第一次
    打開dashboard的那個時間點，不保證是收盤後)，抓到的yfinance「今天」報價本身就是盤中
    某一刻的即時快照，之後直到當天19:00那次補檢查前都不會再更新，等於整天凍結在那個
    時間點——跟2026-08-07那次bars_5min(run_live.py斷線)凍結是同一種問題的另一面。
    兩個資料源都可能是「某個時間點的快照」，差別在於bars_5min的ts本身就是真實時間點，
    可以直接跟現在比對新鮮度；bars_daily的「今天」列沒有這種時間戳記可比，所以規則是：
    bars_5min在INTRADAY_FRESH_WITHIN內有新資料就優先信任它(真的還在即時累積)；否則才退回
    bars_daily的今天列(假設是收盤後補上的，比凍結的盤中快照可靠)；兩者都沒有才用日線
    最後一筆(通常是昨天，或非交易日時的上一個交易日)。這裡的判斷邏輯要跟_reference_date
    完全對齊，不要各自維護一份。"""
    now = pd.Timestamp.now()
    if not today_bars_df.empty and (now - today_bars_df.index[-1]) <= INTRADAY_FRESH_WITHIN:
        return today_bars_df["close"].iloc[-1]

    today = now.normalize()
    today_daily_row = bars_daily_df[bars_daily_df.index >= today]
    if not today_daily_row.empty:
        return today_daily_row["close"].iloc[-1]
    if not today_bars_df.empty:
        return today_bars_df["close"].iloc[-1]
    return bars_daily_df["close"].iloc[-1]


def compute_change(bars_daily_df: pd.DataFrame, today_bars_df: pd.DataFrame):
    """回傳(change, change_pct)：現價(見_current_price)比較上一個交易日收盤的漲跌
    點數/百分比。"""
    reference_date = _reference_date(bars_daily_df, today_bars_df)
    prev_close = _prev_close(bars_daily_df, reference_date)
    if prev_close is None:
        return None, None

    current = _current_price(bars_daily_df, today_bars_df)
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


def build_overview_row_for_symbol(config: Config, symbol: str) -> dict:
    """單一股票的總覽列——2026-08-18拆成per-symbol函式(原本build_overview_rows整個
    watchlist一起算，dashboard只能整包快取)：▲▼移動排序/移除/新增股票都只需要那一支
    受影響，跟其他股票的指標數值完全無關，拆成per-symbol後dashboard可以比照
    _compute_track_record_for_symbol同一套per-symbol快取，移動/刪除不用再強制重算
    整個觀察清單的RSI/MACD/KD等指標(10年資料量下這曾經是移動/刪除變慢的主因)。
    name自己查(跟_compute_track_record_for_symbol同一套慣例)，不用呼叫端傳，介面
    更單純——呼叫端(dashboard)不用另外準備一份code->name對照表當快取鍵的一部分。"""
    rsi_params = config.strategy_params.get("rsi", {})
    macd_params = config.strategy_params.get("macd", {})
    bollinger_params = config.strategy_params.get("bollinger", {})
    volume_params = config.strategy_params.get("volume_anomaly", {})
    kd_params = config.strategy_params.get("kd", {})

    with connect(config.db_path) as conn:
        name_row = conn.execute("SELECT name FROM symbols WHERE code = ?", (symbol,)).fetchone()
        name = (name_row["name"] if name_row else None) or "—"
        bars = bars_to_dataframe(fetch_bars_daily(conn, symbol), ts_field="date")
        if bars.empty:
            return _empty_row(symbol, name)

        flows = fetch_institutional_flows(conn, symbol)
        merged = attach_institutional_flows(bars, flows)
        today_bars = bars_to_dataframe(fetch_bars_5min_latest_day(conn, symbol), ts_field="ts")

    change, change_pct = compute_change(bars, today_bars)
    prev_close = _prev_close(bars, _reference_date(bars, today_bars))

    # bars["close"]最後一筆可能是daily_update盤中抓到、之後整天凍結的快照(見
    # _current_price docstring)，蓋成跟「漲跌」同一套算法算出來的現價，RSI/MACD/
    # 布林通道/均線比較才會跟「漲跌」對得起來，不會顯示自相矛盾的數字。
    close = bars["close"].copy()
    close.iloc[-1] = _current_price(bars, today_bars)
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

    return {
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


def build_overview_rows(config: Config) -> list[dict]:
    with connect(config.db_path) as conn:
        symbol_rows = fetch_watchlist(conn)
    return [build_overview_row_for_symbol(config, r["code"]) for r in symbol_rows]


def build_strategy_recommendations(config: Config) -> list[dict]:
    """觀察清單裡每支股票、每個策略在MAX_SIGNAL_AGE_DAYS(100天)內觸發過的每一個事件都各自
    列一列——不是只留最後一個。2026-08-08使用者指出：只顯示最後一個事件會把「已經出場」
    的舊買進訊號整個蓋掉，例如8299的chip_momentum 7/21買進、7/29停損賣出，如果只顯示
    最後一個(賣出)，使用者在「模擬交易紀錄」看到7/21的買進卻在這張表找不到對應，會覺得
    兩張表對不起來。改成有觸發就留著：一次進出場如果都在100天內，會各自變成一列(一列
    買進、一列賣出)，不是合併或互相蓋掉。「買進策略」跟「賣出策略」剛好一個有填一個留白
    (同一個事件不會同時是買又是賣)。「觸發價格」是那天的收盤價(策略真正判斷買/賣的依據)，
    「現價」是今天的收盤價，兩個分開放才看得出來「當時觸發之後，現在漲跌多少」。

    跟build_paper_trades一樣要跳過disabled_strategies——不然會出現「這支股票這個策略
    已經被排除、不會實際通知了」，畫面上卻還在建議買進的矛盾。

    2026-08-18拆成build_strategy_recommendations_for_symbol(單一股票)+這裡的薄迴圈，
    理由跟build_paper_trades同一套：dashboard可以per-symbol快取，切群組不用重算整個
    觀察清單。"""
    with connect(config.db_path) as conn:
        symbol_rows = fetch_watchlist(conn)
    rows = []
    for r in symbol_rows:
        rows.extend(build_strategy_recommendations_for_symbol(config, r["code"]))
    return rows


def build_strategy_recommendations_for_symbol(config: Config, symbol: str) -> list[dict]:
    """單一股票的買進/賣出策略訊號——見build_strategy_recommendations docstring說明
    整體邏輯。"""
    rows: list[dict] = []
    now = datetime.now()
    with connect(config.db_path) as conn:
        name_row = conn.execute("SELECT name FROM symbols WHERE code = ?", (symbol,)).fetchone()
        name = (name_row["name"] if name_row else None) or "—"
        bars = bars_to_dataframe(fetch_bars_daily(conn, symbol), ts_field="date")
        if bars.empty:
            return rows

        flows = fetch_institutional_flows(conn, symbol)
        merged = attach_institutional_flows(bars, flows)
        merged = attach_monthly_revenue_growth(merged, [dict(r) for r in fetch_monthly_revenue(conn, symbol)])
        today_bars = bars_to_dataframe(fetch_bars_5min_latest_day(conn, symbol), ts_field="ts")
        current_price = _round_or_none(_current_price(bars, today_bars))
        disabled = set(get_disabled_strategies(conn, symbol))

    for strategy_name in NOTIFIABLE_STRATEGIES:
        if strategy_name in disabled:
            continue
        strategy = STRATEGY_REGISTRY.get(strategy_name)
        if strategy is None:
            continue
        events = strategy.evaluate(symbol, merged, config.strategy_params.get(strategy_name, {}))
        recent_events = [e for e in events if (now - e.ts).days <= MAX_SIGNAL_AGE_DAYS]

        for event in recent_events:
            is_buy = event.direction == Direction.BUY
            rows.append(
                {
                    "代號": symbol,
                    "名稱": name,
                    "買進策略": strategy_name if is_buy else "",
                    "賣出策略": strategy_name if not is_buy else "",
                    "觸發價格": _round_or_none(event.price),
                    "現價": current_price,
                    "觸發日期": event.ts.strftime("%Y-%m-%d"),
                }
            )

    return rows


def build_paper_trades(config: Config, start_date: str = "2026-07-01") -> list[dict]:
    """模擬交易紀錄：從start_date開始，NOTIFIABLE_STRATEGIES每個策略每次BUY訊號配對到
    下一個SELL訊號就當作賣出，純粹照著訊號模擬記錄買賣價位跟報酬率，不是真的下單——
    給使用者觀察這幾個策略實際表現用。start_date當天之前已經在場內的部位不算(從那天
    開始當作空手重新起算，跟simulate_round_trips本來的配對邏輯一致：先篩選事件範圍
    再配對)。還沒配到出場的部位標記「持有中」，「賣出價位」留空(還沒真的賣)，另外用
    「現價」欄位算未實現報酬率——兩者分開列，不能讓「持有中」那列的賣出價位看起來
    像已經賣掉了。大多數NOTIFIABLE_STRATEGIES是一買配一賣的形狀，用simulate_round_trips
    配對；分批出場的策略(is_scaleout_strategy()判斷為True，目前是golden_cross_scaleout
    的ma_scaleout模式、bullish_divergence的enable_tiered_profit)改用
    simulate_scaleout_trades，一筆ScaleoutTrade拆成「半倉」「剩餘半倉」兩列顯示(見
    build_paper_trades_for_symbol)，讓使用者看得到兩次分批出場各自的價位，不是合併成
    一個平均數字。

    這裡跟_compute_track_records不一樣：那裡是故意忽略排除清單(給使用者看「為什麼」
    被排除的歷史全貌)，這裡是模擬「照現在的設定實際會不會被通知」，所以個股已經被
    disabled_strategies排除的策略要跳過，不列進模擬交易——不然會看到「策略明明已經
    被排除了，畫面上卻還在模擬買賣」這種矛盾。

    2026-08-15使用者要求：同樣道理，全市場同產業寬度斷路器(circuit_breaker.py)擋掉的
    BUY訊號也不該出現在這裡——不然會看到「這支股票這個訊號當時實際上不會被通知」卻還在
    模擬買賣的矛盾，跟disabled_strategies是同一種問題。斷路器只擋BUY(SELL永遠不擋、
    既有部位一樣可以出場)，所以只過濾events裡的BUY方向，不動SELL。斷路器狀態要用
    compute_breadth_series+replay_active_state逐日回放「當時」的on/off(不能只看
    app_settings存的『現在』狀態)，且shift(1)一天再拿來擋——正式環境裡斷路器狀態要
    收盤後才更新、隔天才生效(見circuit_breaker.py的refresh_industry_states/
    load_active_state)，同一天的收盤資料不能拿來擋當天已經觸發的訊號，不然等於
    look-ahead。個股自己是否跌破月線則用當天(含)以前的收盤價，跟訊號本身用同一天
    收盤價判斷是一致的因果性，不是look-ahead。

    CIRCUIT_BREAKER_EXEMPT_STRATEGIES裡的策略完全跳過這個過濾(見circuit_breaker.py
    同名常數的說明)——bullish_divergence調校後實測2026-07~08的進場訊號被斷路器擋下
    比例是100%，包括後來漲了23%~41%的大贏家，因為「自己也跌破月線」對抄底策略來說
    根本是進場前提、不是警訊，跟run_live.py的即時通知邏輯要一致，不然模擬交易紀錄會
    比實際通知更悲觀。

    2026-08-18拆成build_paper_trades_for_symbol(單一股票)+這裡的薄迴圈——原本整個
    watchlist一起算，dashboard只能整包快取(30秒過期後任何操作都要重算全部股票x全部
    策略x10年資料，是切群組還是偶爾覺得慢的主因)，拆成per-symbol後dashboard可以
    比照build_overview_row_for_symbol同一套per-symbol快取，切群組只需要那個群組
    實際涵蓋的股票，不用管其他股票有沒有過期。"""
    with connect(config.db_path) as conn:
        symbol_rows = fetch_watchlist(conn)
    rows = []
    for r in symbol_rows:
        rows.extend(build_paper_trades_for_symbol(config, r["code"], start_date))
    return rows


def build_paper_trades_for_symbol(config: Config, symbol: str, start_date: str = "2026-07-01") -> list[dict]:
    """單一股票的模擬交易紀錄——見build_paper_trades docstring說明整體邏輯。斷路器狀態
    (compute_breadth_series/replay_active_state)在原本整批版本裡是同產業股票共用一份
    breadth_state_cache(同一次呼叫內只算一次)，拆成per-symbol後同產業的股票各自獨立
    算一次；多花的重複計算只發生在每支股票各自「第一次」被dashboard快取記錄時(之後
    同一支股票的結果都直接命中per-symbol快取，不會重算)，用這個一次性成本換「切群組
    不用重算整個觀察清單」的好處，划算。"""
    start = pd.Timestamp(start_date)
    rows: list[dict] = []
    with connect(config.db_path) as conn:
        name_row = conn.execute("SELECT name FROM symbols WHERE code = ?", (symbol,)).fetchone()
        name = (name_row["name"] if name_row else None) or "—"
        bars = bars_to_dataframe(fetch_bars_daily(conn, symbol), ts_field="date")
        if bars.empty:
            return rows

        flows = fetch_institutional_flows(conn, symbol)
        merged = attach_institutional_flows(bars, flows)
        merged = attach_monthly_revenue_growth(merged, [dict(r) for r in fetch_monthly_revenue(conn, symbol)])
        today_bars = bars_to_dataframe(fetch_bars_5min_latest_day(conn, symbol), ts_field="ts")
        current_price = _current_price(bars, today_bars)
        disabled = set(get_disabled_strategies(conn, symbol))

        industry_code = fetch_all_industry_codes(conn).get(symbol)
        own_ma = None
        own_below_ma = None
        effective_active_state = None
        if industry_code is not None:
            breadth_pct = compute_breadth_series(conn, industry_code, config.circuit_breaker_ma_period)
            active_state = replay_active_state(
                breadth_pct, config.circuit_breaker_enter_threshold, config.circuit_breaker_exit_threshold
            )
            effective_active_state = active_state.shift(1).fillna(False)
            if config.circuit_breaker_own_ma_period is not None:
                own_ma = merged["close"].rolling(config.circuit_breaker_own_ma_period).mean()
                own_below_ma = merged["close"] < own_ma

    def _buy_suppressed(ts) -> bool:
        if effective_active_state is None:
            return False
        if ts not in effective_active_state.index or not effective_active_state.loc[ts]:
            return False
        if own_ma is None:  # own_ma_period=None(現行)：純看產業寬度，不要求自己也跌破均線，
            return True     # 跟circuit_breaker.is_buy_suppressed()同一套邏輯，見該函式docstring
        if ts not in own_ma.index or pd.isna(own_ma.loc[ts]):
            return False
        return bool(own_below_ma.loc[ts])

    for strategy_name in NOTIFIABLE_STRATEGIES:
        if strategy_name in disabled:
            continue
        strategy = STRATEGY_REGISTRY.get(strategy_name)
        if strategy is None:
            continue
        events = strategy.evaluate(symbol, merged, config.strategy_params.get(strategy_name, {}))
        if strategy_name not in CIRCUIT_BREAKER_EXEMPT_STRATEGIES:
            events = [e for e in events if not (e.direction == Direction.BUY and _buy_suppressed(e.ts))]
        events_since = [e for e in events if e.ts >= start]
        if not events_since:
            continue

        base_row = {"代號": symbol, "名稱": name, "策略": strategy_name}

        if is_scaleout_strategy(strategy_name, config.strategy_params.get(strategy_name, {})):
            # 一買配兩賣的分批出場策略：拆成「半倉」「剩餘半倉」兩列各自的實際買賣價位/
            # 報酬率，不用blended_exit_price合併成一個平均數字——使用者在這張表看到的
            # 應該是真實發生過的兩次交易動作，跟策略訊號本身的detail("賣出一半"/
            # 「賣出剩餘一半")一致。
            trades, still_open = simulate_scaleout_trades(events_since)
            for st in trades:
                for leg_label, exit_ts, exit_price in [
                    ("半倉", st.exit1_ts, st.exit1_price),
                    ("剩餘半倉", st.exit2_ts, st.exit2_price),
                ]:
                    rows.append(
                        {
                            **base_row,
                            "策略": f"{strategy_name}({leg_label})",
                            "買進日期": st.entry_ts.strftime("%Y-%m-%d"),
                            "買進價位": _round_or_none(st.entry_price),
                            "賣出日期": exit_ts.strftime("%Y-%m-%d"),
                            "賣出價位": _round_or_none(exit_price),
                            "現價": _round_or_none(current_price),
                            "報酬率(%)": _round_or_none((exit_price - st.entry_price) / st.entry_price * 100),
                            "狀態": "已平倉",
                        }
                    )
            if still_open:
                entry = still_open["entry"]
                exits = still_open["exits"]
                if exits:
                    half_exit = exits[0]
                    rows.append(
                        {
                            **base_row,
                            "策略": f"{strategy_name}(半倉)",
                            "買進日期": entry.ts.strftime("%Y-%m-%d"),
                            "買進價位": _round_or_none(entry.price),
                            "賣出日期": half_exit.ts.strftime("%Y-%m-%d"),
                            "賣出價位": _round_or_none(half_exit.price),
                            "現價": _round_or_none(current_price),
                            "報酬率(%)": _round_or_none((half_exit.price - entry.price) / entry.price * 100),
                            "狀態": "已平倉",
                        }
                    )
                    remaining_label = "剩餘半倉"
                else:
                    remaining_label = "全倉"
                rows.append(
                    {
                        **base_row,
                        "策略": f"{strategy_name}({remaining_label})",
                        "買進日期": entry.ts.strftime("%Y-%m-%d"),
                        "買進價位": _round_or_none(entry.price),
                        "賣出日期": None,
                        "賣出價位": None,
                        "現價": _round_or_none(current_price),
                        "報酬率(%)": _round_or_none((current_price - entry.price) / entry.price * 100),
                        "狀態": "持有中(未實現)",
                    }
                )
            continue

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
