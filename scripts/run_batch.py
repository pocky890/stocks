"""收盤後全市場批次掃描。手動啟動（收盤後跑一次）。全市場只跑8種純OHLCV策略
（不含institutional_streak——那個只有觀察清單才有三大法人資料）。

用api.daily_quotes()一次拿全市場當天的日OHLCV(~2000檔)，不逐檔呼叫kbars()，
跟twse_client/tpex_client「一次呼叫拿全市場」的模式一致。
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stocks.config import load_config
from stocks.db import bars_to_dataframe, connect, fetch_bars_daily, init_db, insert_bars_daily, insert_signal_events
from stocks.models import Tier
from stocks.notifier import notify_batch_summary
from stocks.shioaji_client import ShioajiClient
from stocks.signal_engine import evaluate_all

SKIP_STRATEGIES = {"institutional_streak"}


def main():
    config = load_config()
    init_db(config.db_path)

    client = ShioajiClient(config)
    client.connect()

    today = date.today()
    print(f"抓取 {today} 全市場日OHLCV...")
    bars = client.fetch_daily_quotes(today)
    if not bars:
        print("今天沒有資料（非交易日？）")
        client.disconnect()
        return
    print(f"共 {len(bars)} 檔")

    with connect(config.db_path) as conn:
        insert_bars_daily(conn, bars)

    all_new_events = []
    for i, bar in enumerate(bars):
        symbol = bar.symbol
        with connect(config.db_path) as conn:
            history = bars_to_dataframe(fetch_bars_daily(conn, symbol), ts_field="date")
            events = evaluate_all(symbol, history, config.strategy_params, tier=Tier.BATCH, skip_strategies=SKIP_STRATEGIES)
            new_events = insert_signal_events(conn, events)
            all_new_events.extend(new_events)

        if (i + 1) % 200 == 0:
            print(f"  進度 {i + 1}/{len(bars)}")

    notify_batch_summary(config, all_new_events)
    client.disconnect()
    print(f"完成，共 {len(all_new_events)} 個新訊號")


if __name__ == "__main__":
    main()
