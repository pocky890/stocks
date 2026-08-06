"""盤中即時5分K訊號監控主迴圈。手動啟動、手動Ctrl+C停止（沒有排程）。
連線走Shioaji模擬模式（報價/K棒是真實市場資料，只有下單/成交是模擬帳本，
這個系統本來就不下單）。"""
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stocks.bar_aggregator import BarAggregator, market_hour_boundaries
from stocks.config import load_config
from stocks.db import connect, fetch_bars_5min, fetch_bars_daily, fetch_watchlist, init_db, insert_bars_5min, insert_signal_events
from stocks.db import bars_to_dataframe
from stocks.models import Tier
from stocks.notifier import notify_connectivity, notify_symbol_signals
from stocks.shioaji_client import ShioajiClient
from stocks.signal_engine import evaluate_all


def sleep_until(target: datetime) -> None:
    remaining = (target - datetime.now()).total_seconds()
    if remaining > 0:
        time.sleep(remaining)


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
                events = evaluate_all(symbol, bars, config.strategy_params, tier=Tier.REALTIME)
                new_events = insert_signal_events(conn, events)
            if new_events:
                with connect(config.db_path) as conn:
                    daily_bars = bars_to_dataframe(fetch_bars_daily(conn, symbol), ts_field="date")
                notify_symbol_signals(config, symbol, symbol_names.get(symbol, ""), new_events, daily_bars)
                print(f"  {symbol} {bucket_end.strftime('%H:%M')} 觸發 {len(new_events)} 個訊號")

    client.disconnect()
    print("收盤，連線已關閉")


if __name__ == "__main__":
    main()
