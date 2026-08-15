"""收盤後批次掃描。手動啟動（收盤後跑一次）。2026-08-15使用者要求後，只處理觀察清單
股票，不再對全市場~2000檔評估/通知——太雜訊。跑除institutional_streak以外的所有策略
（那個還是只服務觀察清單自己），但NOTIFIABLE_STRATEGIES(atr_breakout/chip_momentum/
trust_momentum/trend_following/breakout/golden_cross_scaleout/long_swing/
bullish_divergence/capitulation_reversal)另外跳過——這幾個策略對觀察清單股票只在
run_live.py的13:20檢查發通知，這裡再評估一次會重複通知同一天同一檔股票的同一個訊號。
單一指標類策略(RSI/MACD/KD/均線交叉...)照樣評估、寫進signal_events(訊號紀錄頁籤看得到)，
但本來就不發通知，不受影響。

api.daily_quotes()/三大法人API都沒有「只查特定股票」的接口，只能一次拿全市場資料
(~2000檔OHLCV + TWSE/TPEx各一次籌碼呼叫)，篩選出觀察清單再寫入DB/評估——全市場的
部分不落地，只是過濾用，所以多抓這份資料不會多打2000次API。

2026-08-16新增：同一次全市場daily_quotes()呼叫，順便篩出「跟觀察清單同產業」的股票
收盤價存進industry_closes，餵給circuit_breaker.py算「這個產業全市場有幾成股票跌破
自己20日均線」，收盤後這裡順便重算一次斷路器的開關狀態(run_live.py隔天盤中只讀狀態、
不重算)。這批股票要先跑過scripts/populate_industry_codes.py，symbols表裡才會有
industry_code可以比對，不然這裡篩出來是空的(不會crash，只是斷路器暫時沒有任何產業
資料可用，等於形同沒開)。
"""
import sys
from datetime import date
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stocks import tpex_client, twse_client
from stocks.circuit_breaker import refresh_industry_states
from stocks.config import load_config
from stocks.db import (
    attach_institutional_flows,
    bars_to_dataframe,
    connect,
    fetch_all_industry_codes,
    fetch_bars_daily,
    fetch_institutional_flows,
    fetch_watchlist,
    get_disabled_strategies,
    init_db,
    insert_bars_daily,
    insert_industry_closes,
    insert_institutional_flows,
    insert_signal_events,
    prune_industry_closes,
)
from stocks.models import Tier
from stocks.notifier import NOTIFIABLE_STRATEGIES
from stocks.shioaji_client import ShioajiClient
from stocks.signal_engine import evaluate_all

SKIP_STRATEGIES = {"institutional_streak"}  # 這個還是只服務觀察清單


def main():
    config = load_config()
    init_db(config.db_path)

    client = ShioajiClient(config)
    client.connect()

    with connect(config.db_path) as conn:
        watchlist = {row["code"] for row in fetch_watchlist(conn)}
        industry_codes = fetch_all_industry_codes(conn)  # {code: industry_code}，含觀察清單
        # 跟populate_industry_codes.py寫進去的全市場同產業peer股票

    today = date.today()
    print(f"抓取 {today} 日OHLCV(全市場一次呼叫，篩選觀察清單 {len(watchlist)} 檔)...")
    all_quotes = client.fetch_daily_quotes(today)
    bars = [b for b in all_quotes if b.symbol in watchlist]
    if not bars:
        print("今天沒有資料（非交易日？）")
        client.disconnect()
        return
    print(f"共 {len(bars)} 檔")

    with connect(config.db_path) as conn:
        insert_bars_daily(conn, bars)

    industry_rows = [
        {"symbol": b.symbol, "date": today.strftime("%Y-%m-%d"), "industry_code": industry_codes[b.symbol], "close": b.close}
        for b in all_quotes
        if b.symbol in industry_codes
    ]
    print(f"同產業收盤價(全市場一次呼叫篩選，供斷路器算寬度用) {len(industry_rows)} 檔")
    with connect(config.db_path) as conn:
        if industry_rows:
            insert_industry_closes(conn, industry_rows)
        prune_industry_closes(conn)
        state = refresh_industry_states(conn, config)
    active_industries = [code for code, active in state.items() if active]
    print(f"斷路器狀態更新：目前生效中的產業代碼 {active_industries or '(無)'}")

    print("抓取三大法人資料(全市場一次呼叫，篩選觀察清單)...")
    # 上市/上櫃各自獨立try/except：任何一個來源連線失敗(TPEx的SSL已知不穩定)都不該讓
    # 整支批次腳本中斷(沒抓到籌碼還是要繼續跑完OHLCV策略)，只影響那一塊資料。
    flows = []
    try:
        flows += twse_client.fetch_institutional_flows_for_date(today.strftime("%Y-%m-%d"))
    except requests.RequestException as exc:
        print(f"  上市籌碼抓取失敗，跳過：{exc}")
    try:
        flows += tpex_client.fetch_institutional_flows_latest()
    except requests.RequestException as exc:
        print(f"  上櫃籌碼抓取失敗(TPEx SSL偶爾不穩定)，跳過：{exc}")
    flows = [f for f in flows if f["symbol"] in watchlist]
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
            # NOTIFIABLE_STRATEGIES已經由run_live.py的13:20檢查對觀察清單股票發通知，
            # 這裡跳過避免同一天同一檔股票同一個訊號重複發兩次通知。
            skip = SKIP_STRATEGIES | NOTIFIABLE_STRATEGIES | set(get_disabled_strategies(conn, symbol))
            events = evaluate_all(symbol, history, config.strategy_params, tier=Tier.BATCH, skip_strategies=skip)
            new_events = insert_signal_events(conn, events)
            all_new_events.extend(new_events)

        if (i + 1) % 200 == 0:
            print(f"  進度 {i + 1}/{len(bars)}")
    client.disconnect()
    print(f"完成，共 {len(all_new_events)} 個新訊號(單一指標，寫進signal_events但不發通知)")


if __name__ == "__main__":
    main()
