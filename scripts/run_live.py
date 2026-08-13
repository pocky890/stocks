"""盤中即時5分K訊號監控主迴圈。平日08:55由Windows排程任務(TWStocks-RunLive)自動啟動，
也可以手動執行/Ctrl+C停止。連線走Shioaji模擬模式（報價/K棒是真實市場資料，只有下單/
成交是模擬帳本，這個系統本來就不下單）。"""
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stocks.bar_aggregator import BarAggregator, market_hour_boundaries
from stocks.config import load_config
from stocks.db import (
    attach_institutional_flows,
    bars_to_dataframe,
    connect,
    fetch_bars_5min,
    fetch_bars_5min_today,
    fetch_bars_daily,
    fetch_institutional_flows,
    fetch_watchlist,
    get_disabled_strategies,
    init_db,
    insert_bars_5min,
    insert_signal_events,
)
from stocks.models import Tier
from stocks.notifier import notify_connectivity, notify_symbol_signals
from stocks.shioaji_client import ShioajiClient
from stocks.signal_engine import evaluate_all
from stocks.strategies import STRATEGY_REGISTRY

# atr_breakout/trend_following/breakout/golden_cross_scaleout是用「N日」概念設計、只拿
# 日線資料驗證過──直接餵5分K的話「10日均線」其實變成10根5分K(~50分鐘)算出來的均線，
# 跟真正的10日均線是兩個不同的數字(曾經導致通知內容自相矛盾：訊號說「跌破10日均線」，
# 趨勢那行卻說「站上10日線」)。這幾個策略改成額外用「歷史日線+今天累積到目前的partial
# 日K」重新評估(見build_daily_bars_with_today)，讓使用者盤中就能收到以真正日線尺度算出來
# 的訊號、有時間下單，不用等收盤。其他單一指標策略(RSI/MACD/KD/均線交叉...)維持吃5分K，
# 那是原始設計就要它們抓盤中短週期訊號，不受這次修正影響。
DAILY_CONCEPT_STRATEGIES = {
    "atr_breakout",
    "trend_following",
    "breakout",
    "golden_cross_scaleout",
}
INTRADAY_STRATEGIES = set(STRATEGY_REGISTRY) - DAILY_CONCEPT_STRATEGIES
# 13:20收盤前10分鐘檢查一次就好，不用每5分鐘都重算：這幾個策略是日線尺度的訊號，
# 一天有一次明確的檢查時間點更好理解，也讓使用者統一在這個時間點知道今天要不要下單，
# 同時保留收盤前的下單時間。
DAILY_CONCEPT_CHECK_HHMM = "13:20"


def sleep_until(target: datetime) -> None:
    remaining = (target - datetime.now()).total_seconds()
    if remaining > 0:
        time.sleep(remaining)


def build_today_partial_bar(rows) -> dict | None:
    """把今天已經累積的5分K聚合成一根「今天到目前為止」的日K
    (open=今天開盤、high/low=至今最高最低、close=最新價、volume=至今總量)。"""
    if not rows:
        return None
    return {
        "open": rows[0]["open"],
        "high": max(r["high"] for r in rows),
        "low": min(r["low"] for r in rows),
        "close": rows[-1]["close"],
        "volume": sum(r["volume"] for r in rows),
    }


def build_daily_bars_with_today(conn, symbol: str) -> pd.DataFrame:
    """給DAILY_CONCEPT_STRATEGIES用的日線序列：歷史日K(已接上三大法人資料，跟
    run_batch.py同樣的接法) + 今天的partial日K。如果run_batch.py已經跑過、bars_daily
    裡已經有今天正式的日K了，就不用補partial的，直接用正式資料。"""
    history = bars_to_dataframe(fetch_bars_daily(conn, symbol), ts_field="date")
    history = attach_institutional_flows(history, fetch_institutional_flows(conn, symbol))

    today = datetime.now().date()
    if not history.empty and history.index[-1].date() >= today:
        return history

    today_bar = build_today_partial_bar(fetch_bars_5min_today(conn, symbol))
    if today_bar is None:
        return history
    today_row = pd.DataFrame([today_bar], index=[pd.Timestamp(today)])
    return pd.concat([history, today_row])


def main():
    config = load_config()
    init_db(config.db_path)

    with connect(config.db_path) as conn:
        watchlist_rows = fetch_watchlist(conn)
    watchlist = [row["code"] for row in watchlist_rows]
    symbol_names = {row["code"]: row["name"] for row in watchlist_rows}
    if not watchlist:
        print("觀察清單是空的，先在dashboard新增股票")
        return
    print(f"觀察清單: {watchlist}")

    client = ShioajiClient(config)
    client.connect()
    print("Shioaji連線成功（模擬模式）")

    aggregator = BarAggregator()
    client.subscribe_ticks(watchlist, aggregator.on_tick)
    print(f"已訂閱 {len(watchlist)} 檔即時報價")

    was_connected = True
    for bucket_end in market_hour_boundaries(datetime.now().date()):
        if bucket_end < datetime.now():
            continue  # 程式是盤中才啟動的，跳過已經過去的邊界
        sleep_until(bucket_end)

        connected = client.ensure_connected()
        if not connected:
            if was_connected:
                notify_connectivity(config, "lost", "Shioaji無回應，正在自動重試")
            was_connected = False
            continue
        if not was_connected:
            notify_connectivity(config, "restored")
        was_connected = True

        new_bars = aggregator.flush_bucket(bucket_end)
        for symbol, bar in new_bars.items():
            with connect(config.db_path) as conn:
                insert_bars_5min(conn, [bar])
                bars = bars_to_dataframe(fetch_bars_5min(conn, symbol, limit=200), ts_field="ts")
                skip = DAILY_CONCEPT_STRATEGIES | set(get_disabled_strategies(conn, symbol))
                events = evaluate_all(symbol, bars, config.strategy_params, tier=Tier.REALTIME, skip_strategies=skip)
                new_events = insert_signal_events(conn, events)
            if new_events:
                with connect(config.db_path) as conn:
                    daily_bars = bars_to_dataframe(fetch_bars_daily(conn, symbol), ts_field="date")
                notify_symbol_signals(config, symbol, symbol_names.get(symbol, ""), new_events, daily_bars)
                print(f"  {symbol} {bucket_end.strftime('%H:%M')} 觸發 {len(new_events)} 個訊號")

        # atr_breakout/trend_following/breakout/golden_cross_scaleout只在13:20這個時間點
        # 檢查全觀察清單一次(不看new_bars，即使某檔今天這一格沒有成交也照樣檢查)，不受
        # 個股tick頻率影響。
        if bucket_end.strftime("%H:%M") == DAILY_CONCEPT_CHECK_HHMM:
            now = datetime.now()
            for symbol in watchlist:
                with connect(config.db_path) as conn:
                    daily_bars_with_today = build_daily_bars_with_today(conn, symbol)
                    skip = INTRADAY_STRATEGIES | set(get_disabled_strategies(conn, symbol))
                    raw_events = evaluate_all(
                        symbol, daily_bars_with_today, config.strategy_params, tier=Tier.REALTIME, skip_strategies=skip
                    )
                    # 2026-08-13發現：daily_bars_with_today是對整段歷史重新跑一次edge-trigger，
                    # 一旦「今天」這筆日K的加入讓ATR/唐奇安通道之類的滾動窗口跟昨天算的不一樣，
                    # 會回頭冒出幾筆日期是前幾天、但資料庫裡還沒有的「新」事件(不是真的今天發生)
                    # ——只留日期真的是今天的，且時間戳記換成現在真正檢查的時間(不是bars index
                    # 構造出來的午夜0點)，通知上才會顯示合理的觸發時間。
                    events = [replace(e, ts=now) for e in raw_events if e.ts.date() == now.date()]
                    new_events = insert_signal_events(conn, events)
                if new_events:
                    with connect(config.db_path) as conn:
                        daily_bars = bars_to_dataframe(fetch_bars_daily(conn, symbol), ts_field="date")
                    notify_symbol_signals(config, symbol, symbol_names.get(symbol, ""), new_events, daily_bars)
                    print(f"  {symbol} {bucket_end.strftime('%H:%M')} 觸發 {len(new_events)} 個訊號(日線)")

    client.disconnect()
    print("收盤，連線已關閉")


if __name__ == "__main__":
    main()
