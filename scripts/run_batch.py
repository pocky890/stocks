"""收盤後全市場批次掃描。手動啟動（收盤後跑一次）。全市場跑9種策略（不含
institutional_streak——那個目前只服務觀察清單，其他策略包含buy_formula/sell_formula
都吃得到當天全市場的三大法人資料）。

用api.daily_quotes()一次拿全市場當天的日OHLCV(~2000檔)，不逐檔呼叫kbars()；三大法人
資料同樣是TWSE/TPEx「一次呼叫拿全市場」，不是逐檔查詢，所以多抓這份資料不會多打
2000次API，只是多兩次呼叫(TWSE一次、TPEx一次)。
"""
import sys
from datetime import date
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stocks import tpex_client, twse_client
from stocks.config import load_config
from stocks.db import (
    attach_institutional_flows,
    bars_to_dataframe,
    connect,
    fetch_bars_daily,
    fetch_institutional_flows,
    fetch_watchlist,
    init_db,
    insert_bars_daily,
    insert_institutional_flows,
    insert_signal_events,
)
from stocks.models import Tier
from stocks.notifier import notify_batch_summary
from stocks.shioaji_client import ShioajiClient
from stocks.signal_engine import evaluate_all

SKIP_STRATEGIES = {"institutional_streak"}  # 這個還是只服務觀察清單


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

    print("抓取三大法人資料(全市場，上市+上櫃各一次呼叫)...")
    # 上市/上櫃各自獨立try/except：任何一個來源連線失敗(TPEx的SSL已知不穩定)都不該讓
    # 整支批次腳本中斷(沒抓到籌碼還是要繼續跑完OHLCV策略跟notify)，只影響那一塊資料。
    flows = []
    try:
        flows += twse_client.fetch_institutional_flows_for_date(today.strftime("%Y-%m-%d"))
    except requests.RequestException as exc:
        print(f"  上市籌碼抓取失敗，跳過：{exc}")
    try:
        flows += tpex_client.fetch_institutional_flows_latest()
    except requests.RequestException as exc:
        print(f"  上櫃籌碼抓取失敗(TPEx SSL偶爾不穩定)，跳過：{exc}")
    print(f"共 {len(flows)} 檔籌碼資料")
    with connect(config.db_path) as conn:
        if flows:
            insert_institutional_flows(conn, flows)

    all_new_events = []
    for i, bar in enumerate(bars):
        symbol = bar.symbol
        with connect(config.db_path) as conn:
            history = bars_to_dataframe(fetch_bars_daily(conn, symbol), ts_field="date")
            history = attach_institutional_flows(history, fetch_institutional_flows(conn, symbol))
            events = evaluate_all(symbol, history, config.strategy_params, tier=Tier.BATCH, skip_strategies=SKIP_STRATEGIES)
            new_events = insert_signal_events(conn, events)
            all_new_events.extend(new_events)

        if (i + 1) % 200 == 0:
            print(f"  進度 {i + 1}/{len(bars)}")

    with connect(config.db_path) as conn:
        watchlist = {row["code"] for row in fetch_watchlist(conn)}
    notify_batch_summary(config, all_new_events, watchlist)
    client.disconnect()
    print(f"完成，共 {len(all_new_events)} 個新訊號")


if __name__ == "__main__":
    main()
