from collections import defaultdict
from datetime import date

import pandas as pd

from stocks.config import Config
from stocks.indicators import sma
from stocks.models import Direction, SignalEvent
from stocks.telegram_client import send_message

DIRECTION_LABEL = {Direction.BUY: "買", Direction.SELL: "賣"}
MAX_BATCH_SYMBOLS_LISTED = 30
# 只有這幾個「進場/出場邏輯完整、可以直接照著做」的策略會推播——單一指標(RSI/MACD/KD/
# 均線交叉...)本身誤判率較高，不再各自觸發通知，但照樣會寫進signal_events(訊號紀錄頁籤
# 看得到全部)。極簡買賣公式(buy_formula/sell_formula)回測後整體表現墊底，已經移除。
NOTIFIABLE_STRATEGIES = {
    "atr_breakout",
    "chip_momentum",
    "trust_momentum",
    "trend_following",
    "breakout",
    "golden_cross_scaleout",
    "long_swing",
}
_MA_PERIODS = (5, 10, 20, 60)
_MA_NAMES = {20: "月", 60: "季"}  # 5、10維持數字講法，20/60叫月線/季線，跟dashboard命名一致


def _format_ma_group(periods: list[int]) -> str:
    day_nums = [str(p) for p in periods if p not in _MA_NAMES]
    named = [_MA_NAMES[p] for p in periods if p in _MA_NAMES]
    parts = []
    if day_nums:
        parts.append("、".join(day_nums) + "日線")
    if named:
        parts.append("、".join(named) + "線")
    return "、".join(parts)


def _trend_text(daily_close: pd.Series) -> str:
    """現價站上/跌破哪幾條均線——跟其他checklist項目不一樣，這是「當下狀態」不是
    「剛剛觸發的事件」，所以不計入「觸發訊號n項」，獨立一行呈現。資料不足(例如新股票
    還沒有60天歷史)就回傳空字串，呼叫端自己決定要不要印這一行。"""
    ma_values = {p: sma(daily_close, p).iloc[-1] for p in _MA_PERIODS}
    valid = {p: v for p, v in ma_values.items() if not pd.isna(v)}
    if not valid:
        return ""

    latest_close = daily_close.iloc[-1]
    above = [p for p in _MA_PERIODS if p in valid and latest_close > valid[p]]
    below = [p for p in _MA_PERIODS if p in valid and latest_close <= valid[p]]

    parts = []
    if above:
        parts.append(f"站上{_format_ma_group(above)}")
    if below:
        parts.append(f"跌破{_format_ma_group(below)}")
    return "、".join(parts)


def notify_symbol_signals(
    config: Config, symbol: str, name: str, events: list[SignalEvent], daily_bars: pd.DataFrame
) -> bool:
    """Combine every triggered strategy for one symbol at one point in time into a
    single Telegram message, per the aggregation design (list which strategies fired,
    no weighted/scored judgment).

    只通知NOTIFIABLE_STRATEGIES這幾個策略——單一指標(RSI/MACD/KD/均線交叉...)不再
    各自觸發通知，那些訊號照樣寫進signal_events，只是不推播。
    real-time這條路只服務觀察清單股票(run_live.py只訂閱觀察清單的tick)，所以這裡
    不需要另外判斷是否在觀察清單——跟notify_batch_summary(服務全市場)不一樣。

    趨勢(站上/跌破哪些均線)是額外資訊——那是daily_bars隨時能算的「當下狀態」，不是
    編造的，所以額外加一行，但不計入觸發項目數。daily_bars要是日線(不是5分K)，
    5/10/20/60的均線才是有意義的天數。"""
    events = [e for e in events if e.strategy in NOTIFIABLE_STRATEGIES]
    if not events:
        return True

    buy_events = [e for e in events if e.direction == Direction.BUY]
    sell_events = [e for e in events if e.direction == Direction.SELL]
    if buy_events and sell_events:
        title, emoji = "🟡 訊號同時觸發", "📊"
    elif buy_events:
        title, emoji = "🟢 策略進場觸發", "📊"
    else:
        title, emoji = "🔴 警戒！出場訊號", "⚠️"

    label = f"{symbol} {name}" if name else symbol
    lines = [
        f"【{title}】",
        f"標的：{label}",
        f"現價：${events[0].price:.1f}",
        f"時間：{events[0].ts.strftime('%Y-%m-%d %H:%M')}",
        "",
        f"{emoji} 觸發訊號（{len(events)}項）：",
    ]
    lines += [f"[V] {e.detail or e.strategy}" for e in buy_events + sell_events]

    trend = _trend_text(daily_bars["close"]) if not daily_bars.empty else ""
    if trend:
        lines += ["", f"📈 趨勢：{trend}"]

    return send_message(config.telegram_bot_token, config.telegram_chat_id, "\n".join(lines))


def notify_connectivity(config: Config, event_type: str, detail: str = "") -> bool:
    label = {"lost": "連線中斷", "restored": "連線已恢復"}.get(event_type, event_type)
    text = f"[系統] {label}"
    if detail:
        text += f" — {detail}"
    return send_message(config.telegram_bot_token, config.telegram_chat_id, text)


def notify_batch_summary(config: Config, events: list[SignalEvent], watchlist: set[str]) -> bool:
    """One digest message per EOD batch run: symbol counts, capped listing.

    只通知「今天」真的觸發的訊號，即使events裡混了更早的：edge-triggered策略每次都重新
    掃過整段歷史，第一次幫某檔股票建訊號表、或排程斷過幾天沒跑時，整段歷史的舊crossing
    會被db.insert_signal_events()當成「新的」一次全灌進來，摘要不篩今天的話會被歷史
    累積灌爆(曾經一次收到769檔、內容只有股票代號+策略縮寫，完全看不出是不是今天的訊號)。

    只通知NOTIFIABLE_STRATEGIES這幾個策略，單一指標不再各自觸發通知。賣出訊號只對
    觀察清單內的股票有意義(你在關注/持有的才需要知道要不要賣)，不在watchlist裡的股票
    只送買進通知——這也是run_batch.py全市場掃描要傳watchlist進來的原因。注意：
    chip_momentum需要三大法人資料，目前只有watchlist股票有這份資料(run_batch.py的
    SKIP_STRATEGIES會跳過全市場的institutional_streak，但chip_momentum本身沒被跳過，
    只是全市場股票缺資料時會自動優雅降級回傳空清單)，所以現階段非watchlist股票的
    chip_momentum訊號實際上不會產生——這裡的watchlist篩選是為未來全市場也接上籌碼
    資料時預留的正確行為，不是現在就能生效的功能。"""
    today_events = [e for e in events if e.ts.date() == date.today() and e.strategy in NOTIFIABLE_STRATEGIES]
    today_events = [e for e in today_events if e.direction == Direction.BUY or e.symbol in watchlist]

    by_symbol: dict[str, list[SignalEvent]] = defaultdict(list)
    for e in today_events:
        by_symbol[e.symbol].append(e)

    if not by_symbol:
        return send_message(config.telegram_bot_token, config.telegram_chat_id, "[收盤批次掃描] 今天沒有符合條件的股票")

    lines = [f"[收盤批次掃描] 共 {len(by_symbol)} 檔觸發訊號:"]
    for i, (symbol, symbol_events) in enumerate(by_symbol.items()):
        if i >= MAX_BATCH_SYMBOLS_LISTED:
            lines.append(f"...還有 {len(by_symbol) - MAX_BATCH_SYMBOLS_LISTED} 檔，詳見dashboard")
            break
        details = "、".join(f"{e.detail} @{e.price:.1f}" for e in symbol_events)
        lines.append(f"  {symbol}: {details}")

    return send_message(config.telegram_bot_token, config.telegram_chat_id, "\n".join(lines))
