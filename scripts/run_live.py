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
from stocks.circuit_breaker import is_buy_suppressed, load_active_state
from stocks.config import load_config
from stocks.daily_update import check_and_update
from stocks.db import (
    attach_institutional_flows,
    bars_to_dataframe,
    connect,
    fetch_all_industry_codes,
    fetch_bars_5min,
    fetch_bars_5min_today,
    fetch_bars_daily,
    fetch_ex_dividend_schedule,
    fetch_institutional_flows,
    fetch_signal_events,
    fetch_watchlist,
    get_disabled_strategies,
    init_db,
    insert_bars_5min,
    insert_signal_events,
)
from stocks.models import Direction, Tier
from stocks.notifier import (
    NOTIFIABLE_STRATEGIES,
    notify_connectivity,
    notify_ex_dividend_today,
    notify_reminder,
    notify_symbol_signals,
)
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


def todays_cash_dividend(conn, symbol: str, today) -> float:
    """今天如果剛好是這支股票的除息日，回傳現金股利金額，否則回傳0——2026-08-15使用者
    發現：除息當天交易所會把參考價機制性扣掉股利金額(除息參考價=前一天收盤-股利)，
    這不是公司真的下跌，但即時監控用的bars_daily歷史高點/停損線是「除息前」算的，
    今天的即時報價(Shioaji原始報價，不像bars_daily是yfinance還原過的)卻已經反映了
    除息後的價格，兩者基準對不上，會讓停損誤判成跌破。這裡只處理現金股利，股票股利
    (配股)牽涉股數變動，先不處理(範圍縮小，之後真的要做再另外處理)。"""
    today_str = today.isoformat()
    for row in fetch_ex_dividend_schedule(conn, symbol):
        if row["ex_date"] == today_str and row["cash_dividend"]:
            return float(row["cash_dividend"])
    return 0.0


def build_daily_bars_with_today(conn, symbol: str) -> pd.DataFrame:
    """給DAILY_CONCEPT_STRATEGIES用的日線序列：歷史日K(已接上三大法人資料，跟
    run_batch.py同樣的接法) + 今天的partial日K。如果run_batch.py已經跑過、bars_daily
    裡已經有今天正式的日K了，就不用補partial的，直接用正式資料。

    今天如果剛好是除息日(見todays_cash_dividend)，「今天」這根K棒的open/high/low/close
    全部加回股利金額，讓它跟歷史資料(除息前)站在同一個價格基準比較，不會被除息造成的
    機制性下跌誤判成真的跌破停損——隔天bars_daily會被_refresh_price_data用yfinance重新
    抓一次，屆時「今天」就會變成正式的還原後歷史資料，不再需要這裡的補償，所以這個
    加回去的動作只需要做在「今天」這一根，不用往回處理更早的資料。"""
    history = bars_to_dataframe(fetch_bars_daily(conn, symbol), ts_field="date")
    history = attach_institutional_flows(history, fetch_institutional_flows(conn, symbol))

    today = datetime.now().date()
    dividend_addback = todays_cash_dividend(conn, symbol, today)

    if not history.empty and history.index[-1].date() >= today:
        if dividend_addback:
            history = history.copy()
            today_mask = history.index.date == today
            for col in ("open", "high", "low", "close"):
                history.loc[today_mask, col] += dividend_addback
        return history

    today_bar = build_today_partial_bar(fetch_bars_5min_today(conn, symbol))
    if today_bar is None:
        return history
    if dividend_addback:
        today_bar = {
            **today_bar,
            **{col: today_bar[col] + dividend_addback for col in ("open", "high", "low", "close")},
        }
    today_row = pd.DataFrame([today_bar], index=[pd.Timestamp(today)])
    return pd.concat([history, today_row])


def main():
    config = load_config()
    init_db(config.db_path)

    # 08:55排程啟動時不一定有人剛好開過dashboard，除權息預告表(跟股價/籌碼一樣)可能是
    # 好幾天前的舊資料——這裡主動跑一次check_and_update確保今天要用的除權息清單是新的，
    # 不依賴使用者剛好開過dashboard才會更新。
    check_and_update(config)

    with connect(config.db_path) as conn:
        watchlist_rows = fetch_watchlist(conn)
        today_str = datetime.now().date().isoformat()
        ex_dividend_today = []
        for row in watchlist_rows:
            for sched in fetch_ex_dividend_schedule(conn, row["code"]):
                if sched["ex_date"] == today_str:
                    ex_dividend_today.append({**dict(sched), "name": row["name"]})
        # 產業代碼/斷路器開關狀態一整個盤中時段內不會變(industry_code只在新增股票時變，
        # 斷路器狀態只有run_batch.py收盤後才會更新)，這裡讀一次存在記憶體裡整場重複用，
        # 不用每個5分K tick、每支股票都重新查一次DB。
        industry_codes = fetch_all_industry_codes(conn)
        circuit_breaker_state = load_active_state(conn)
    watchlist = [row["code"] for row in watchlist_rows]
    symbol_names = {row["code"]: row["name"] for row in watchlist_rows}
    if not watchlist:
        print("觀察清單是空的，先在dashboard新增股票")
        return
    print(f"觀察清單: {watchlist}")

    if ex_dividend_today:
        notify_ex_dividend_today(config, ex_dividend_today)
        print(f"今日除權息 {len(ex_dividend_today)} 檔，已發送提醒")

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
                    ex_dividend_dates = {r["ex_date"] for r in fetch_ex_dividend_schedule(conn, symbol)}
                notify_symbol_signals(config, symbol, symbol_names.get(symbol, ""), new_events, daily_bars, ex_dividend_dates)
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
                # 斷路器只擋「要不要推播」，不影響signal_events寫入——訊號紀錄頁籤要看得到
                # 完整歷史，被擋掉的BUY一樣算「這個策略真的觸發過」，只是不推播Telegram。
                notifiable_events = [
                    e
                    for e in new_events
                    if e.direction != Direction.BUY
                    or not is_buy_suppressed(symbol, industry_codes, circuit_breaker_state, daily_bars_with_today, config.circuit_breaker_ma_period)
                ]
            if notifiable_events:
                with connect(config.db_path) as conn:
                    daily_bars = bars_to_dataframe(fetch_bars_daily(conn, symbol), ts_field="date")
                    ex_dividend_dates = {r["ex_date"] for r in fetch_ex_dividend_schedule(conn, symbol)}
                notify_symbol_signals(config, symbol, symbol_names.get(symbol, ""), notifiable_events, daily_bars, ex_dividend_dates)
                print(f"  {symbol} {bucket_end.strftime('%H:%M')} 觸發 {len(notifiable_events)} 個訊號(日線)")
            if new_events and not notifiable_events:
                print(f"  {symbol} {bucket_end.strftime('%H:%M')} 觸發 {len(new_events)} 個訊號(日線)，全部被產業斷路器擋下(只記錄不推播)")

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
                    ex_dividend_dates = {r["ex_date"] for r in fetch_ex_dividend_schedule(conn, symbol)}
                if not latest_per_strategy or daily_bars_with_today.empty:
                    continue
                current_price = daily_bars_with_today["close"].iloc[-1]
                still_valid = [
                    r
                    for r in latest_per_strategy.values()
                    if (r["direction"] == Direction.BUY.value and current_price >= r["price"])
                    or (r["direction"] == Direction.SELL.value and current_price <= r["price"])
                ]
                # 斷路器也要擋這裡：不然BUY通知當下被斷路器擋掉不推播，13:20卻又直接讀
                # signal_events把同一筆補發出去，等於繞過斷路器(2026-08-16使用者確認
                # 要修這個漏洞)。SELL永遠不擋，跟即時檢查那邊的規則一致。
                still_valid = [
                    r
                    for r in still_valid
                    if r["direction"] != Direction.BUY.value
                    or not is_buy_suppressed(
                        symbol, industry_codes, circuit_breaker_state, daily_bars_with_today, config.circuit_breaker_ma_period
                    )
                ]
                if still_valid:
                    notify_reminder(config, symbol, symbol_names.get(symbol, ""), still_valid, current_price, ex_dividend_dates)
                    print(f"  {symbol} {bucket_end.strftime('%H:%M')} 提醒 {len(still_valid)} 個尚未解除的訊號")

    client.disconnect()
    print("收盤，連線已關閉")


if __name__ == "__main__":
    main()
