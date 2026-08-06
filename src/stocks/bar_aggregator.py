from datetime import datetime, timedelta

from stocks.models import Bar


class BarAggregator:
    """把tick緩衝成K棒。每個symbol各自累積(ts, price, volume)，flush_bucket()把
    緩衝內ts早於bucket_end的tick聚合成一根K棒(open=第一筆,high=max,low=min,
    close=最後一筆,volume=加總)後清空對應緩衝；沒有tick的symbol這輪就不出現在
    回傳結果裡（不補K棒），呼叫端(run_live.py)自然就不會為它跑signal_engine。"""

    def __init__(self):
        self._buffers: dict[str, list[tuple[datetime, float, int]]] = {}

    def on_tick(self, symbol: str, ts: datetime, price: float, volume: int) -> None:
        self._buffers.setdefault(symbol, []).append((ts, price, volume))

    def flush_bucket(self, bucket_end: datetime) -> dict[str, Bar]:
        bars: dict[str, Bar] = {}
        for symbol, ticks in list(self._buffers.items()):
            in_bucket = [t for t in ticks if t[0] < bucket_end]
            if not in_bucket:
                continue

            prices = [p for _, p, _ in in_bucket]
            bars[symbol] = Bar(
                symbol=symbol,
                ts=bucket_end,
                open=prices[0],
                high=max(prices),
                low=min(prices),
                close=prices[-1],
                volume=sum(v for _, _, v in in_bucket),
            )

            remaining = [t for t in ticks if t[0] >= bucket_end]
            if remaining:
                self._buffers[symbol] = remaining
            else:
                del self._buffers[symbol]

        return bars


def market_hour_boundaries(date, open_time="09:00", close_time="13:30", interval_minutes=5):
    """回傳當天從第一個滿interval_minutes的邊界到收盤的K棒邊界時間點
    (預設09:05, 09:10, ..., 13:30)。"""
    open_hour, open_minute = (int(x) for x in open_time.split(":"))
    close_hour, close_minute = (int(x) for x in close_time.split(":"))
    start = datetime(date.year, date.month, date.day, open_hour, open_minute) + timedelta(minutes=interval_minutes)
    end = datetime(date.year, date.month, date.day, close_hour, close_minute)

    boundaries = []
    current = start
    while current <= end:
        boundaries.append(current)
        current += timedelta(minutes=interval_minutes)
    return boundaries
