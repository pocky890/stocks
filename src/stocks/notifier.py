from collections import defaultdict
from datetime import date

import pandas as pd

from stocks.config import Config
from stocks.indicators import sma
from stocks.models import Direction, SignalEvent
from stocks.strategies import strategy_label
from stocks.telegram_client import send_message

DIRECTION_LABEL = {Direction.BUY: "買", Direction.SELL: "賣"}
# run_live.py主迴圈每個5分K bucket都會寫入這個app_settings key(現在時間的isoformat())，
# scripts/check_run_live_heartbeat.py(獨立排程，跟run_live.py生命週期無關)定期讀取，
# 太久沒更新就代表run_live.py已經停止/卡住(不管是被中止還是踩到未來還沒修過的網路卡死
# bug)——2026-08-17使用者實際踩到run_live.py被中止3小時多都沒人發現的案例後新增。
RUN_LIVE_HEARTBEAT_KEY = "run_live_last_heartbeat"
RUN_LIVE_STALL_ALERTED_KEY = "run_live_stall_alerted"
# check_run_live_heartbeat.py用的緩衝期狀態：第一次發現心跳不新鮮的時間點，等下一輪還是
# 不新鮮才真的警告，避免開機延遲時run_live.py還在啟動中的空窗被誤判成「已經停止」。
RUN_LIVE_STALE_SINCE_KEY = "run_live_stale_since"
MAX_BATCH_SYMBOLS_LISTED = 30
# 只有這幾個「進場/出場邏輯完整、可以直接照著做」的策略會推播——單一指標(RSI/MACD/KD/
# 均線交叉...)本身誤判率較高，不再各自觸發通知，但照樣會寫進signal_events(訊號紀錄頁籤
# 看得到全部)。極簡買賣公式(buy_formula/sell_formula)回測後整體表現墊底，已經移除。
# bullish_divergence/capitulation_reversal(2026-08-15新增，「抓最低點」系列)：進出場邏輯
# 完整(進場條件各自見策略docstring，出場統一15%移動停損)，經scripts/backtest_bottom_
# pickers.py全觀察清單10年回測驗證後加入。chip_reversal_fast同批加入，2026-08-15使用者
# 要求整支策略拿掉(不只是不通知)，已從STRATEGY_REGISTRY/config.json完全移除。
NOTIFIABLE_STRATEGIES = {
    "atr_breakout",
    "chip_momentum",
    "trust_momentum",
    "trend_following",
    "breakout",
    "golden_cross",
    "long_swing",
    "bullish_divergence",
    "capitulation_reversal",
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


def _format_signal_line(e: SignalEvent, entry) -> str:
    """單筆觸發訊號的顯示文字。entry是db.find_last_entry_event()查到的進場紀錄(sqlite3.
    Row，需要ts/price欄位)——出場(SELL)且找得到對應進場時，額外加一行進場日期/價位跟
    這趟報酬率，讓使用者不用自己回頭查是什麼時候買的、賺賠多少。找不到進場紀錄(例如
    這個(symbol,strategy)是第一次出場、沒有更早的進場對照)就只顯示原本的觸發原因，
    不擋主要通知。"""
    base = f"[V] {strategy_label(e.strategy)}：{e.detail or e.strategy}"
    if e.direction != Direction.SELL or entry is None:
        return base
    entry_date = entry["ts"][:10]
    entry_price = entry["price"]
    return_pct = (e.price - entry_price) / entry_price * 100
    return f"{base}\n      進場：{entry_date} @{entry_price:.1f} → 出場@{e.price:.1f}，報酬率：{return_pct:+.1f}%"


def notify_symbol_signals(
    config: Config,
    symbol: str,
    name: str,
    events: list[SignalEvent],
    daily_bars: pd.DataFrame,
    ex_dividend_dates: set[str] = frozenset(),
    entry_events: dict | None = None,
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
    5/10/20/60的均線才是有意義的天數。

    ex_dividend_dates：這支股票已知/預告中的除權息日期集合(db.fetch_ex_dividend_schedule
    查出來的ex_date，是提前公告的資料，呼叫端在除權息當天之前就查得到)——2026-08-15
    使用者發現：除權息當天交易所會把參考價機制性地扣掉股利金額(除息參考價=前一天收盤-
    股利)，這不是公司真的下跌，但停損/停利邏輯只看得到股價、看不到使用者應該收到的股息，
    可能誤判成跌破停損。這裡只是在通知裡多加一行提醒，讓使用者自己判斷這次觸發有沒有
    參考價值，不是自動排除或改變訊號本身——訊號紀錄/歷史績效統計都不受影響。

    entry_events：{strategy: db.find_last_entry_event()查到的進場紀錄}，只需要包含
    這次events裡SELL的策略——2026-08-19使用者要求出場通知要看得到進場日期/價位跟報酬率，
    不能只看到出場原因。呼叫端(run_live.py)要在還沒關閉的connection裡先查好，這裡不
    自己開DB連線。"""
    events = [e for e in events if e.strategy in NOTIFIABLE_STRATEGIES]
    if not events:
        return True

    entry_events = entry_events or {}
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
    lines += [_format_signal_line(e, entry_events.get(e.strategy)) for e in buy_events + sell_events]

    sell_ex_div_dates = {e.ts.strftime("%Y-%m-%d") for e in sell_events} & set(ex_dividend_dates)
    if sell_ex_div_dates:
        lines += [
            "",
            f"⚠️ 注意：{'、'.join(sorted(sell_ex_div_dates))} 是這支股票的除權息日，賣出訊號"
            "可能是除息參考價機制性下跌(前一天收盤-股利)，不一定是真的下跌，建議自行確認。",
        ]

    trend = _trend_text(daily_bars["close"]) if not daily_bars.empty else ""
    if trend:
        lines += ["", f"📈 趨勢：{trend}"]

    return send_message(config.telegram_bot_token, config.telegram_chat_id, "\n".join(lines))


def notify_reminder(
    config: Config,
    symbol: str,
    name: str,
    rows: list,
    current_price: float,
    ex_dividend_dates: set[str] = frozenset(),
    entry_events: dict | None = None,
) -> bool:
    """13:20固定提醒：今天已經通知過的訊號，如果現在價格還是跟當時觸發方向一致(BUY還沒
    跌破、SELL還沒回升)，代表狀況還沒解除，使用者可能還沒處理，額外提醒一次——跟
    notify_symbol_signals的「新訊號剛觸發」語意不同，這裡是「舊訊號還沒解除」，2026-08-14
    使用者要求的：盤中觸發就先通知一次，13:20如果還是同一個方向再提醒一次，不用等使用者
    自己記得回頭看。rows是signal_events查出來的sqlite3.Row(或相容dict)，需要symbol/
    strategy/direction/price/ts欄位。

    ex_dividend_dates同notify_symbol_signals——rows都是今天的資料(呼叫端已經篩過)，
    今天如果剛好是這支股票的除權息日、又有賣出訊號還沒解除，一樣加提醒。

    entry_events同notify_symbol_signals：{strategy: db.find_last_entry_event()查到的
    進場紀錄}，只需要包含這次rows裡SELL的策略。"""
    if not rows:
        return True
    entry_events = entry_events or {}
    buy_rows = [r for r in rows if r["direction"] == Direction.BUY.value]
    sell_rows = [r for r in rows if r["direction"] == Direction.SELL.value]
    if buy_rows and sell_rows:
        title = "⏰ 提醒：買進+賣出訊號都還沒解除"
    elif buy_rows:
        title = "⏰ 提醒：買進訊號還沒解除"
    else:
        title = "⏰ 提醒：賣出訊號還沒解除"

    label = f"{symbol} {name}" if name else symbol
    lines = [
        f"【{title}】",
        f"標的：{label}",
        f"現價：${current_price:.1f}",
        "",
        "今天已經通知過，現在看起來還是同一個方向：",
    ]
    for row in buy_rows + sell_rows:
        is_buy = row["direction"] == Direction.BUY.value
        tag = "🟢買" if is_buy else "🔴賣"
        verb = "還沒跌破" if is_buy else "還沒回升"
        ts_text = row["ts"][11:16] if len(row["ts"]) >= 16 else row["ts"]
        line = f"[{tag}] {strategy_label(row['strategy'])}：{ts_text}觸發@{row['price']:.1f}，{verb}"
        entry = None if is_buy else entry_events.get(row["strategy"])
        if entry is not None:
            entry_date = entry["ts"][:10]
            entry_price = entry["price"]
            return_pct = (row["price"] - entry_price) / entry_price * 100
            line += f"\n      進場：{entry_date} @{entry_price:.1f}，報酬率：{return_pct:+.1f}%"
        lines.append(line)

    if sell_rows:
        today_str = sell_rows[0]["ts"][:10]
        if today_str in ex_dividend_dates:
            lines += [
                "",
                f"⚠️ 注意：今天({today_str})是這支股票的除權息日，賣出訊號可能是除息參考價"
                "機制性下跌，不一定是真的下跌，建議自行確認。",
            ]

    return send_message(config.telegram_bot_token, config.telegram_chat_id, "\n".join(lines))


def notify_resolved(
    config: Config,
    symbol: str,
    name: str,
    rows: list,
    current_price: float,
    entry_events: dict | None = None,
) -> bool:
    """13:20固定檢查的另一面：今天通知過的訊號，現在方向已經反轉(BUY跌破了/SELL回升了)，
    代表狀況解除了——2026-08-20使用者發現：原本這種情況notify_reminder()的still_valid
    篩選會直接把它濾掉，導致完全沒有任何通知，使用者只能靠「今天下午沒收到提醒」自己
    推論已經解除，看不出來到底是解除了還是系統忘了檢查。這裡補上明確的「已解除」通知，
    跟notify_reminder互斥(同一筆signal_events紀錄，現在只會落在其中一邊)。

    不套用斷路器篩選——斷路器是用來擋「要不要送出新的BUY動作訊號」，跟「告知一個已經
    發生過的狀況現在解除了」是不同性質的訊息，不應該被同一套規則擋掉。

    entry_events同notify_reminder：{strategy: db.find_last_entry_event()查到的進場
    紀錄}，只需要包含這次rows裡SELL的策略——賣出訊號解除後，讓使用者還是看得到這趟
    倉位目前的進場基準/報酬率參考，不是解除了就什麼資訊都不給。"""
    if not rows:
        return True
    entry_events = entry_events or {}
    buy_rows = [r for r in rows if r["direction"] == Direction.BUY.value]
    sell_rows = [r for r in rows if r["direction"] == Direction.SELL.value]
    if buy_rows and sell_rows:
        title = "✅ 買進+賣出訊號都已解除"
    elif buy_rows:
        title = "✅ 買進訊號已解除"
    else:
        title = "✅ 賣出訊號已解除"

    label = f"{symbol} {name}" if name else symbol
    lines = [
        f"【{title}】",
        f"標的：{label}",
        f"現價：${current_price:.1f}",
        "",
        "今天通知過的訊號，現在狀況已經解除：",
    ]
    for row in buy_rows + sell_rows:
        is_buy = row["direction"] == Direction.BUY.value
        tag = "🟢買" if is_buy else "🔴賣"
        verb = "已跌破，機會消失" if is_buy else "已回升，風險解除"
        ts_text = row["ts"][11:16] if len(row["ts"]) >= 16 else row["ts"]
        line = f"[{tag}] {strategy_label(row['strategy'])}：{ts_text}觸發@{row['price']:.1f}，{verb}"
        entry = None if is_buy else entry_events.get(row["strategy"])
        if entry is not None:
            entry_date = entry["ts"][:10]
            entry_price = entry["price"]
            return_pct = (row["price"] - entry_price) / entry_price * 100
            line += f"\n      進場：{entry_date} @{entry_price:.1f}，報酬率：{return_pct:+.1f}%"
        lines.append(line)

    return send_message(config.telegram_bot_token, config.telegram_chat_id, "\n".join(lines))


def notify_ex_dividend_today(config: Config, rows: list[dict]) -> bool:
    """早上一次性通知：今天觀察清單裡有哪幾檔要除權息、金額多少——2026-08-15使用者要求，
    這樣盤中如果剛好看到停損觸發，心裡已經有個底「這支今天有除息，可能是股價機制性
    下跌」，不用等到訊號真的觸發才第一次知道。rows每筆需要symbol/name/cash_dividend/
    stock_dividend_ratio/detail欄位(跟db.fetch_ex_dividend_schedule欄位一致，呼叫端
    篩過ex_date==今天再傳進來)。沒有任何一檔今天除權息就什麼都不送(不用每天洗版一句
    「今天沒有除權息」)。"""
    if not rows:
        return True

    lines = [f"【📅 今日除權息提醒】共 {len(rows)} 檔："]
    for row in rows:
        label = f"{row['symbol']} {row['name']}" if row.get("name") else row["symbol"]
        parts = []
        if row.get("cash_dividend"):
            parts.append(f"現金股利{row['cash_dividend']:.2f}元")
        if row.get("stock_dividend_ratio"):
            parts.append(f"股票股利{row['stock_dividend_ratio']}")
        detail = "、".join(parts) if parts else (row.get("detail") or "除權息")
        lines.append(f"  {label}：{detail}")
    lines.append("")
    lines.append("今天如果看到賣出訊號觸發，記得對照這份清單，可能是除息造成的價格下跌，不一定是真的下跌。")

    return send_message(config.telegram_bot_token, config.telegram_chat_id, "\n".join(lines))


def notify_connectivity(config: Config, event_type: str, detail: str = "") -> bool:
    label = {
        "lost": "連線中斷",
        "restored": "連線已恢復",
        "run_live_stalled": "run_live.py心跳中斷",
        "run_live_recovered": "run_live.py心跳恢復",
    }.get(event_type, event_type)
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
        details = "、".join(f"{strategy_label(e.strategy)}：{e.detail} @{e.price:.1f}" for e in symbol_events)
        lines.append(f"  {symbol}: {details}")

    return send_message(config.telegram_bot_token, config.telegram_chat_id, "\n".join(lines))
