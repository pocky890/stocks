from collections import defaultdict
from datetime import date

import pandas as pd

from stocks.config import Config
from stocks.indicators import sma
from stocks.models import Direction, SignalEvent
from stocks.telegram_client import send_message

DIRECTION_LABEL = {Direction.BUY: "買", Direction.SELL: "賣"}
MAX_BATCH_SYMBOLS_LISTED = 30
MIN_STRATEGIES_TO_NOTIFY = 2  # 單一指標誤判率較高，同一時間點至少要有這麼多策略同方向
# 一起觸發才推播；沒達到門檻的還是會記錄進signal_events(歷史/訊號紀錄頁籤不受影響)，
# 只是不會推到Telegram轟炸手機。
_MA_PERIODS = (5, 10, 20, 60)
_MA_NAMES = {20: "月", 60: "季"}  # 5、10維持數字講法，20/60叫月線/季線，跟dashboard命名一致


def _strong_enough(events: list[SignalEvent], min_strategies: int) -> list[SignalEvent]:
    """只留下「同方向至少min_strategies個策略一起觸發」的那一側；買/賣分開算，因為
    2個互相矛盾的訊號(1買1賣)不該互相加總湊到門檻——那不是confirmation，是分歧。
    兩側都不到門檻就整批丟棄(回傳空list)。"""
    buy = [e for e in events if e.direction == Direction.BUY]
    sell = [e for e in events if e.direction == Direction.SELL]
    kept = []
    if len(buy) >= min_strategies:
        kept += buy
    if len(sell) >= min_strategies:
        kept += sell
    return kept


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
    config: Config,
    symbol: str,
    name: str,
    events: list[SignalEvent],
    daily_bars: pd.DataFrame,
    min_strategies: int = MIN_STRATEGIES_TO_NOTIFY,
) -> bool:
    """Combine every triggered strategy for one symbol at one point in time into a
    single Telegram message, per the aggregation design (list which strategies fired,
    no weighted/scored judgment).

    只列「真的觸發的」項目當作「觸發訊號」，不虛構「符合幾分之幾」的分數或停損建議這類
    目前系統沒有算過的數字——9種策略性質不一(有些是當下狀態如RSI，有些是瞬間交叉如
    MACD)，硬湊成統一的checklist會做出沒依據的判斷。趨勢(站上/跌破哪些均線)例外——那是
    daily_bars隨時能算的「當下狀態」，不是編造的，所以額外加一行，但不計入觸發項目數。
    daily_bars要是日線(不是5分K)，5/10/20/60的均線才是有意義的天數。

    單一策略觸發不推播(min_strategies門檻)——同一時間點沒有其他策略同方向confirm，
    誤判率較高，不值得跳出來吵手機，但仍算是events的一部分，DB照樣記錄。"""
    events = _strong_enough(events, min_strategies)
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


def notify_batch_summary(config: Config, events: list[SignalEvent], min_strategies: int = MIN_STRATEGIES_TO_NOTIFY) -> bool:
    """One digest message per EOD batch run: symbol counts, capped listing.

    只通知「今天」真的觸發的訊號，即使events裡混了更早的：edge-triggered策略每次都重新
    掃過整段歷史，第一次幫某檔股票建訊號表、或排程斷過幾天沒跑時，整段歷史的舊crossing
    會被db.insert_signal_events()當成「新的」一次全灌進來，摘要不篩今天的話會被歷史
    累積灌爆(曾經一次收到769檔、內容只有股票代號+策略縮寫，完全看不出是不是今天的訊號)。

    同一檔股票當天只有1個策略觸發的話也不列進摘要(min_strategies門檻，跟
    notify_symbol_signals一致)——單一指標誤判率較高，不夠格佔一行版面。"""
    today_events = [e for e in events if e.ts.date() == date.today()]

    by_symbol: dict[str, list[SignalEvent]] = defaultdict(list)
    for e in today_events:
        by_symbol[e.symbol].append(e)
    by_symbol = {symbol: strong for symbol, symbol_events in by_symbol.items() if (strong := _strong_enough(symbol_events, min_strategies))}

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
