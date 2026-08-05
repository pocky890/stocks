from collections import defaultdict

from stocks.config import Config
from stocks.models import Direction, SignalEvent
from stocks.telegram_client import send_message

DIRECTION_LABEL = {Direction.BUY: "買", Direction.SELL: "賣"}
MAX_BATCH_SYMBOLS_LISTED = 30


def _format_event_line(e: SignalEvent) -> str:
    label = DIRECTION_LABEL[e.direction]
    detail = f"({e.detail})" if e.detail else ""
    return f"{e.strategy} {detail} → {label}"


def notify_symbol_signals(config: Config, symbol: str, events: list[SignalEvent]) -> bool:
    """Combine every triggered strategy for one symbol at one point in time into a
    single Telegram message, per the aggregation design (list which strategies fired,
    no weighted/scored judgment)."""
    if not events:
        return True

    ts = events[0].ts.strftime("%H:%M")
    lines = [f"{symbol} {ts} 觸發訊號:"]
    lines += [f"  - {_format_event_line(e)}" for e in events]
    return send_message(config.telegram_bot_token, config.telegram_chat_id, "\n".join(lines))


def notify_connectivity(config: Config, event_type: str, detail: str = "") -> bool:
    label = {"lost": "連線中斷", "restored": "連線已恢復"}.get(event_type, event_type)
    text = f"[系統] {label}"
    if detail:
        text += f" — {detail}"
    return send_message(config.telegram_bot_token, config.telegram_chat_id, text)


def notify_batch_summary(config: Config, events: list[SignalEvent]) -> bool:
    """One digest message per EOD batch run: symbol counts, capped listing."""
    if not events:
        return send_message(config.telegram_bot_token, config.telegram_chat_id, "[收盤批次掃描] 今天沒有符合條件的股票")

    by_symbol: dict[str, list[SignalEvent]] = defaultdict(list)
    for e in events:
        by_symbol[e.symbol].append(e)

    lines = [f"[收盤批次掃描] 共 {len(by_symbol)} 檔觸發訊號:"]
    for i, (symbol, symbol_events) in enumerate(by_symbol.items()):
        if i >= MAX_BATCH_SYMBOLS_LISTED:
            lines.append(f"...還有 {len(by_symbol) - MAX_BATCH_SYMBOLS_LISTED} 檔，詳見dashboard")
            break
        strategies = "、".join(f"{e.strategy}({DIRECTION_LABEL[e.direction]})" for e in symbol_events)
        lines.append(f"  {symbol}: {strategies}")

    return send_message(config.telegram_bot_token, config.telegram_chat_id, "\n".join(lines))
