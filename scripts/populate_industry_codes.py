"""一次性(或觀察清單新增其他產業的股票後手動重跑)腳本：幫觀察清單股票標記官方產業
分類代碼(twse/tpex公司名錄的「產業別」欄位，例如"24"=半導體業)，並把全市場「同產業」
的股票也寫進symbols表(is_watchlist=0，不會出現在觀察清單畫面)，供circuit_breaker.py
算「這個產業全市場有幾成股票跌破自己20日均線」用。查證過FinMind的TaiwanStockInfo
industry_category對上市半導體股仍停留在舊式「電子工業」分類沒更新，故意不用FinMind，
直接用twse_client/tpex_client官方公司名錄。

手動執行，觀察清單加了新產業的股票之後要重跑一次，讓新產業的全市場同業清單也建立起來
——daily_update.add_symbol_to_watchlist新增股票時只會標記那支股票自己的產業代碼，
不會順便把該產業全市場的股票都拉進來，那件事只有這支腳本會做。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stocks import tpex_client, twse_client
from stocks.config import load_config
from stocks.db import connect, fetch_watchlist, init_db, set_industry_code, upsert_industry_universe


def main():
    config = load_config()
    init_db(config.db_path)

    print("抓取TWSE/TPEx公司名錄(含產業別)...")
    directory = []
    for name_source, fetch in [("TWSE", twse_client.fetch_company_directory), ("TPEx", tpex_client.fetch_company_directory)]:
        try:
            rows = fetch()
            for r in rows:
                r["market"] = name_source
            directory += rows
            print(f"  {name_source}: {len(rows)} 檔")
        except Exception as exc:
            print(f"  {name_source} 抓取失敗，跳過：{exc}")
    by_code = {r["symbol"]: r for r in directory}

    with connect(config.db_path) as conn:
        watchlist = [(row["code"], row["name"]) for row in fetch_watchlist(conn)]

        target_industry_codes = set()
        print("\n標記觀察清單股票的產業代碼：")
        for code, name in watchlist:
            info = by_code.get(code)
            if info is None or not info.get("industry_code"):
                print(f"  {code} {name}: 在公司名錄找不到產業別，跳過")
                continue
            set_industry_code(conn, code, info["industry_code"])
            target_industry_codes.add(info["industry_code"])
            print(f"  {code} {name}: {info['industry_code']}")

        print(f"\n觀察清單涵蓋的產業代碼: {sorted(target_industry_codes)}")

        peers = [
            {"code": r["symbol"], "name": r["name"], "market": r["market"], "industry_code": r["industry_code"]}
            for r in directory
            if r.get("industry_code") in target_industry_codes
        ]
        upsert_industry_universe(conn, peers)
        print(f"已寫入全市場同產業股票共 {len(peers)} 檔(供斷路器算產業寬度用，不會出現在觀察清單畫面)")


if __name__ == "__main__":
    main()
