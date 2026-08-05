"""暫用腳本：Shioaji金鑰到手前，用yfinance抓範例歷史日K資料填入bars_daily，
讓strategies/backtest/dashboard有真實資料可以跑。之後直接替換成shioaji_client.fetch_kbars()，
其他程式碼（signal_engine, backtest, dashboard）都不需要改。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import argparse

from stocks.config import load_config
from stocks.db import connect, init_db, insert_bars_daily, upsert_symbol
from stocks.yfinance_client import fetch_symbol_bars

DEFAULT_SYMBOLS = ["2330", "2317", "2454", "2308", "2882"]


def main():
    parser = argparse.ArgumentParser(description="填充範例歷史日K資料")
    parser.add_argument("symbols", nargs="*", default=DEFAULT_SYMBOLS, help="台股代號，例如 2330")
    parser.add_argument("--period", default="1y", help="yfinance period，預設近1年")
    args = parser.parse_args()

    config = load_config()
    init_db(config.db_path)

    for symbol in args.symbols:
        print(f"抓取 {symbol} 近{args.period}日K...")
        bars = fetch_symbol_bars(symbol, args.period)
        if not bars:
            print(f"  警告：{symbol} 沒有抓到資料")
            continue
        with connect(config.db_path) as conn:
            insert_bars_daily(conn, bars)
            upsert_symbol(conn, symbol, market="TWSE", is_watchlist=True)
        print(f"  已寫入 {len(bars)} 筆")


if __name__ == "__main__":
    main()
