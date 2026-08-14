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
    fetch_signal_events,
    fetch_watchlist,
    get_disabled_strategies,
    init_db,
    insert_bars_5min,
    insert_signal_events,
)
from stocks.models import Direction, Tier
from stocks.notifier import NOTIFIABLE_STRATEGIES, notify_connectivity, notify_reminder, notify_symbol_signals
from stocks.shioaji_client import ShioajiClient
from stocks.signal_engine import evaluate_all
from stocks.strategies import STRATEGY_REGISTRY

# atr_breakout/trend_following/breakout/golden_cross_scaleout是用「N日」概念設計、只拿
# 日線資料驗證過──直接餵5分K的話「10日均線」其實變成10根5分K(~50分鐘)算出來的均線，
# 跟真正的10日均線是兩個不同的數字(曾經導致通知內容自相矛盾：訊號說「跌破10日均線」，
# 趨勢那行卻說「站上10日線」)。chip_momentum/trust_momentum/long_swing則是需要
# foreign_net/trust_net(三大法人日資料，只有日線join得到)，餵5分K會直接優雅降級回傳
# 空清單。這7個NOTIFIABLE_STRATEGIES因此都改成用「歷史日線(已接上三大法人資料)+今天
# 累積到目前的partial日K」重新評估(見build_daily_bars_with_today)。2026-08-14使用者要求
# 盤中一旦觸發就立即通知(不等收盤前才檢查)，改成每個5分K tick都重新檢查一次全觀察清單，
# 不限制同一天同一檔同一策略只能通知一次——如果股價來回穿越觸發條件，一天可能收到同一
# 策略好幾次通知，這是刻意選擇的行為(run_batch.py收盤後對觀察清單股票不再重複評估這幾個
# 策略，避免收盤後又跟盤中的通知重複)。
DAILY_CONCEPT_STRATEGIES = NOTIFIABLE_STRATEGIES
INTRADAY_STRATEGIES = set(STRATEGY_REGISTRY) - DAILY_CONCEPT_STRATEGIES  # 日線尺度檢查要跳過的集合

# 2026-08-14發現：其他單一指標策略(ma_crossover/rsi/macd/bollinger/volume_anomaly/
# ma_alignment/kd/ma_trend)的期數(5/10/20/60)也是校準給日線用的，一樣被塞進5分K即時
# 迴圈算會失真——「5/10/20日均線」變成「5/10/20根5分K(25/50/100分鐘)均線」，盤中小幅
# 震盪就會頻繁誤觸發(例如多空排列在20分鐘內來回觸發好幾次買賣，看起來莫名其妙)。
# chip_momentum/trust_momentum/long_swing需要foreign_net(只有日線join到三大法人資料)，
# 餵5分K會直接優雅降級回傳空清單，也不該留在這裡跑。5分K即時迴圈只留price_alert：它是
# 「有沒有跨過某個固定價格」，跟granularity無關，任何頻率算都是同一個意思，是唯一真正
# 適合5分K即時評估的策略。其他NOTIFIABLE_STRATEGIES正確的評估路徑是下面每個tick都跑一次
# 的日線尺度檢查，純指標訊號(ma_crossover/rsi/macd/...)則是run_batch.py/dashboard分析裡
# 用真正的日K。
INTRADAY_LIVE_STRATEGIES = {"price_alert"}

# 2026-08-14使用者要求：盤中觸發的通知不代表使用者當下就會照做(可能沒看到/決定先觀望)，
# 系統本身也沒有真的下單、不知道使用者實際上有沒有處理——所以13:20固定再檢查一次「今天
# 通知過的訊號，現在的方向是不是還一樣」(BUY:現價還在觸發價之上；SELL:現價還在觸發價
# 之下)，還一樣就代表狀況沒解除，額外提醒一次，避免使用者忘記處理。這跟前面的即時檢查是
# 兩件獨立的事：即時檢查負責「發現新訊號」，這裡負責「提醒還沒處理的舊訊號」。
REMINDER_CHECK_HHMM = "13:20"


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
                skip = (set(STRATEGY_REGISTRY) - INTRADAY_LIVE_STRATEGIES) | set(get_disabled_strategies(conn, symbol))
                events = evaluate_all(symbol, bars, config.strategy_params, tier=Tier.REALTIME, skip_strategies=skip)
                new_events = insert_signal_events(conn, events)
            if new_events:
                with connect(config.db_path) as conn:
                    daily_bars = bars_to_dataframe(fetch_bars_daily(conn, symbol), ts_field="date")
                notify_symbol_signals(config, symbol, symbol_names.get(symbol, ""), new_events, daily_bars)
                print(f"  {symbol} {bucket_end.strftime('%H:%M')} 觸發 {len(new_events)} 個訊號")

        # 2026-08-14使用者確認：NOTIFIABLE_STRATEGIES(日線尺度)改成每個5分K tick都檢查
        # 全觀察清單一次(不看new_bars，即使某檔這一格沒有成交也照樣檢查)，觸發就立即通知，
        # 不等13:20——使用者要的是「盤中觸發就先送一次」，不限制同一天同一檔同一策略只能
        # 通知一次；如果股價來回穿越觸發條件，一天可能收到同一策略好幾次通知，這是使用者
        # 明確選擇的行為，不是bug。
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

        if bucket_end.strftime("%H:%M") == REMINDER_CHECK_HHMM:
            today_str = now.date().isoformat()
            for symbol in watchlist:
                with connect(config.db_path) as conn:
                    todays_rows = [
                        r
                        for r in fetch_signal_events(conn, symbol=symbol, limit=500)
                        if r["ts"].startswith(today_str) and r["strategy"] in NOTIFIABLE_STRATEGIES
                    ]
                    # 同一個策略今天可能已經觸發過幾次(BUY/SELL來回)，只看最後一次的方向
                    # 才代表「現在」該提醒什麼，不是每一次舊觸發都各提醒一遍。
                    latest_per_strategy = {}
                    for r in sorted(todays_rows, key=lambda r: r["ts"]):
                        latest_per_strategy[r["strategy"]] = r
                    daily_bars_with_today = build_daily_bars_with_today(conn, symbol)
                if not latest_per_strategy or daily_bars_with_today.empty:
                    continue
                current_price = daily_bars_with_today["close"].iloc[-1]
                still_valid = [
                    r
                    for r in latest_per_strategy.values()
                    if (r["direction"] == Direction.BUY.value and current_price >= r["price"])
                    or (r["direction"] == Direction.SELL.value and current_price <= r["price"])
                ]
                if still_valid:
                    notify_reminder(config, symbol, symbol_names.get(symbol, ""), still_valid, current_price)
                    print(f"  {symbol} {bucket_end.strftime('%H:%M')} 提醒 {len(still_valid)} 個尚未解除的訊號")

    client.disconnect()
    print("收盤，連線已關閉")


if __name__ == "__main__":
    main()
