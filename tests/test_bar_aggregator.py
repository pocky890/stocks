from datetime import date, datetime

from stocks.bar_aggregator import BarAggregator, market_hour_boundaries


def test_flush_bucket_aggregates_ticks_into_ohlcv():
    agg = BarAggregator()
    bucket_end = datetime(2026, 1, 5, 9, 5)
    agg.on_tick("2330", datetime(2026, 1, 5, 9, 0, 1), 100.0, 10)
    agg.on_tick("2330", datetime(2026, 1, 5, 9, 2, 0), 105.0, 20)
    agg.on_tick("2330", datetime(2026, 1, 5, 9, 3, 0), 98.0, 5)
    agg.on_tick("2330", datetime(2026, 1, 5, 9, 4, 59), 102.0, 15)

    bars = agg.flush_bucket(bucket_end)

    assert "2330" in bars
    bar = bars["2330"]
    assert bar.open == 100.0
    assert bar.high == 105.0
    assert bar.low == 98.0
    assert bar.close == 102.0
    assert bar.volume == 50
    assert bar.ts == bucket_end


def test_flush_bucket_leaves_ticks_at_or_after_boundary_for_next_bucket():
    agg = BarAggregator()
    first_bucket = datetime(2026, 1, 5, 9, 5)
    second_bucket = datetime(2026, 1, 5, 9, 10)

    agg.on_tick("2330", datetime(2026, 1, 5, 9, 3, 0), 100.0, 10)
    agg.on_tick("2330", datetime(2026, 1, 5, 9, 5, 0), 110.0, 20)  # exactly at boundary -> next bucket

    first = agg.flush_bucket(first_bucket)
    assert first["2330"].close == 100.0
    assert first["2330"].volume == 10

    agg.on_tick("2330", datetime(2026, 1, 5, 9, 7, 0), 115.0, 5)
    second = agg.flush_bucket(second_bucket)
    assert second["2330"].open == 110.0, "the tick exactly at the boundary belongs to the next bucket"
    assert second["2330"].volume == 25


def test_flush_bucket_omits_symbols_with_no_ticks_this_round():
    agg = BarAggregator()
    agg.on_tick("2330", datetime(2026, 1, 5, 9, 1, 0), 100.0, 10)
    bars = agg.flush_bucket(datetime(2026, 1, 5, 9, 5))
    assert "2317" not in bars

    # nothing buffered for 2330 since the last flush -> shouldn't reappear
    bars_again = agg.flush_bucket(datetime(2026, 1, 5, 9, 10))
    assert bars_again == {}


def test_market_hour_boundaries_spans_open_to_close_every_5_minutes():
    boundaries = market_hour_boundaries(date(2026, 1, 5))

    assert boundaries[0] == datetime(2026, 1, 5, 9, 5)
    assert boundaries[-1] == datetime(2026, 1, 5, 13, 30)
    assert len(boundaries) == 54
    assert all((boundaries[i + 1] - boundaries[i]).seconds == 300 for i in range(len(boundaries) - 1))
