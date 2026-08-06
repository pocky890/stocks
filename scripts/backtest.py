"""跑7種策略在歷史資料(bars_daily)上的訊號統計，上線前用來人工判斷訊號頻率是否合理。
不寫入signal_events（避免污染即時/批次的真實歷史紀錄），只印出統計表。
"""
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stocks.config import load_config
from stocks.db import (
    attach_institutional_flows,
    bars_to_dataframe,
    connect,
    fetch_bars_daily,
    fetch_institutional_flows,
    fetch_watchlist,
)
from stocks.models import Tier
from stocks.signal_engine import evaluate_all


def main():
    config = load_config()

    with connect(config.db_path) as conn:
        symbols = [row["code"] for row in fetch_watchlist(conn)]
        bars_by_symbol = {}
        for s in symbols:
            bars = bars_to_dataframe(fetch_bars_daily(conn, s), ts_field="date")
            bars_by_symbol[s] = attach_institutional_flows(bars, fetch_institutional_flows(conn, s))

    if not symbols:
        print("watchlist是空的，先跑 scripts/fetch_historical.py 填資料")
        return

    counts = defaultdict(lambda: defaultdict(int))  # counts[strategy][direction] = n
    per_symbol_counts = defaultdict(lambda: defaultdict(int))  # per_symbol_counts[symbol][strategy] = n

    for symbol, bars in bars_by_symbol.items():
        if bars.empty:
            continue
        events = evaluate_all(symbol, bars, config.strategy_params, tier=Tier.BATCH)
        for e in events:
            counts[e.strategy][e.direction.value] += 1
            per_symbol_counts[symbol][e.strategy] += 1

    n_bars = {s: len(b) for s, b in bars_by_symbol.items()}
    print(f"觀察清單: {symbols}")
    print(f"各股歷史K棒數: {n_bars}\n")

    print("=== 各策略訊號次數統計（全部股票加總） ===")
    for strategy, dirs in counts.items():
        total = sum(dirs.values())
        print(f"  {strategy:16s}  buy={dirs.get('buy', 0):3d}  sell={dirs.get('sell', 0):3d}  total={total:3d}")

    print("\n=== 各股票各策略訊號次數 ===")
    for symbol in symbols:
        print(f"  {symbol}: {dict(per_symbol_counts[symbol])}")


if __name__ == "__main__":
    main()
