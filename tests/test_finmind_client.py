from stocks import finmind_client


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_fetch_institutional_flows_for_range_aggregates_foreign_trust_dealer(monkeypatch):
    payload = {
        "msg": "success",
        "data": [
            {"date": "2026-07-01", "stock_id": "8299", "buy": 1234891, "sell": 1615601, "name": "Foreign_Investor"},
            {"date": "2026-07-01", "stock_id": "8299", "buy": 0, "sell": 0, "name": "Foreign_Dealer_Self"},
            {"date": "2026-07-01", "stock_id": "8299", "buy": 83567, "sell": 266600, "name": "Investment_Trust"},
            {"date": "2026-07-01", "stock_id": "8299", "buy": 72000, "sell": 91200, "name": "Dealer_self"},
            {"date": "2026-07-01", "stock_id": "8299", "buy": 220986, "sell": 283052, "name": "Dealer_Hedging"},
        ],
    }
    monkeypatch.setattr(finmind_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    rows = finmind_client.fetch_institutional_flows_for_range("8299", "2026-07-01", "2026-07-01")

    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "8299"
    assert row["date"] == "2026-07-01"
    assert row["foreign_net"] == 1234891 - 1615601, "Foreign_Investor + Foreign_Dealer_Self(all zero here)"
    assert row["trust_net"] == 83567 - 266600
    assert row["dealer_net"] == (72000 - 91200) + (220986 - 283052), "Dealer_self + Dealer_Hedging combined"
    assert row["total_net"] == row["foreign_net"] + row["trust_net"] + row["dealer_net"]


def test_fetch_institutional_flows_for_range_combines_foreign_investor_and_foreign_dealer(monkeypatch):
    # Foreign_Dealer_Self is usually 0 but should still be added into foreign_net when non-zero,
    # matching twse_client.py's convention of summing the two foreign columns together.
    payload = {
        "msg": "success",
        "data": [
            {"date": "2026-07-01", "stock_id": "2330", "buy": 1000, "sell": 500, "name": "Foreign_Investor"},
            {"date": "2026-07-01", "stock_id": "2330", "buy": 200, "sell": 100, "name": "Foreign_Dealer_Self"},
        ],
    }
    monkeypatch.setattr(finmind_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    rows = finmind_client.fetch_institutional_flows_for_range("2330", "2026-07-01", "2026-07-01")

    assert rows[0]["foreign_net"] == (1000 - 500) + (200 - 100)


def test_fetch_institutional_flows_for_range_returns_multiple_dates_sorted(monkeypatch):
    payload = {
        "msg": "success",
        "data": [
            {"date": "2026-07-02", "stock_id": "8299", "buy": 10, "sell": 5, "name": "Foreign_Investor"},
            {"date": "2026-07-01", "stock_id": "8299", "buy": 20, "sell": 5, "name": "Foreign_Investor"},
        ],
    }
    monkeypatch.setattr(finmind_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    rows = finmind_client.fetch_institutional_flows_for_range("8299", "2026-07-01", "2026-07-02")

    assert [r["date"] for r in rows] == ["2026-07-01", "2026-07-02"]


def test_fetch_institutional_flows_for_range_returns_empty_on_failure_message(monkeypatch):
    payload = {"msg": "error", "data": []}
    monkeypatch.setattr(finmind_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    assert finmind_client.fetch_institutional_flows_for_range("8299", "2026-07-01", "2026-07-02") == []


def test_fetch_valuations_for_range_maps_finmind_fields(monkeypatch):
    payload = {
        "msg": "success",
        "data": [{"date": "2026-07-01", "stock_id": "2330", "PER": 31.86, "dividend_yield": 0.93, "PBR": 10.43}],
    }
    monkeypatch.setattr(finmind_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    rows = finmind_client.fetch_valuations_for_range("2330", "2026-07-01", "2026-07-01")

    assert rows == [
        {"symbol": "2330", "date": "2026-07-01", "pe_ratio": 31.86, "dividend_yield": 0.93, "pb_ratio": 10.43}
    ]


def test_fetch_valuations_for_range_returns_empty_on_failure_message(monkeypatch):
    payload = {"msg": "error", "data": []}
    monkeypatch.setattr(finmind_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    assert finmind_client.fetch_valuations_for_range("2330", "2026-07-01", "2026-07-02") == []


def test_fetch_monthly_revenue_for_range_maps_finmind_fields(monkeypatch):
    payload = {
        "msg": "success",
        "data": [
            {
                "date": "2025-02-01",
                "stock_id": "2330",
                "country": "Taiwan",
                "revenue": 293288038000,
                "revenue_month": 1,
                "revenue_year": 2025,
                "create_time": "",
            }
        ],
    }
    monkeypatch.setattr(finmind_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    rows = finmind_client.fetch_monthly_revenue_for_range("2330", "2025-01-01", "2025-02-28")

    assert rows == [
        {"symbol": "2330", "date": "2025-02-01", "revenue_year": 2025, "revenue_month": 1, "revenue": 293288038000}
    ]


def test_fetch_monthly_revenue_for_range_returns_empty_on_failure_message(monkeypatch):
    payload = {"msg": "error", "data": []}
    monkeypatch.setattr(finmind_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    assert finmind_client.fetch_monthly_revenue_for_range("2330", "2025-01-01", "2025-02-28") == []


def test_fetch_ex_dividend_schedule_for_range_maps_cash_only_event(monkeypatch):
    payload = {
        "msg": "success",
        "data": [
            {
                "date": "2026-03-24",
                "stock_id": "2330",
                "StockEarningsDistribution": 0.0,
                "StockExDividendTradingDate": "",
                "CashEarningsDistribution": 6.00003573,
                "CashExDividendTradingDate": "2026-03-17",
            }
        ],
    }
    monkeypatch.setattr(finmind_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    rows = finmind_client.fetch_ex_dividend_schedule_for_range("2330", "2026-01-01", "2026-12-31")

    assert rows == [
        {
            "symbol": "2330",
            "ex_date": "2026-03-17",
            "cash_dividend": 6.00003573,
            "stock_dividend_ratio": None,
            "detail": "除息",
        }
    ]


def test_fetch_ex_dividend_schedule_for_range_marks_stock_only_event_as_除權(monkeypatch):
    payload = {
        "msg": "success",
        "data": [
            {
                "date": "2026-06-01",
                "stock_id": "2454",
                "StockEarningsDistribution": 0.5,
                "StockExDividendTradingDate": "2026-06-10",
                "CashEarningsDistribution": 0.0,
                "CashExDividendTradingDate": "",
            }
        ],
    }
    monkeypatch.setattr(finmind_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    rows = finmind_client.fetch_ex_dividend_schedule_for_range("2454", "2026-01-01", "2026-12-31")

    assert rows[0]["ex_date"] == "2026-06-10"
    assert rows[0]["cash_dividend"] is None
    assert rows[0]["stock_dividend_ratio"] == "0.50000000"
    assert rows[0]["detail"] == "除權"


def test_fetch_ex_dividend_schedule_for_range_marks_same_date_cash_and_stock_as_除權息(monkeypatch):
    payload = {
        "msg": "success",
        "data": [
            {
                "date": "2026-06-01",
                "stock_id": "2454",
                "StockEarningsDistribution": 0.5,
                "StockExDividendTradingDate": "2026-06-10",
                "CashEarningsDistribution": 2.0,
                "CashExDividendTradingDate": "2026-06-10",
            }
        ],
    }
    monkeypatch.setattr(finmind_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    rows = finmind_client.fetch_ex_dividend_schedule_for_range("2454", "2026-01-01", "2026-12-31")

    assert len(rows) == 1
    assert rows[0]["ex_date"] == "2026-06-10"
    assert rows[0]["cash_dividend"] == 2.0
    assert rows[0]["stock_dividend_ratio"] == "0.50000000"
    assert rows[0]["detail"] == "除權息"


def test_fetch_ex_dividend_schedule_for_range_sorts_by_ex_date(monkeypatch):
    payload = {
        "msg": "success",
        "data": [
            {
                "date": "2026-06-01",
                "stock_id": "2330",
                "StockEarningsDistribution": 0.0,
                "StockExDividendTradingDate": "",
                "CashEarningsDistribution": 6.0,
                "CashExDividendTradingDate": "2026-06-11",
            },
            {
                "date": "2026-03-01",
                "stock_id": "2330",
                "StockEarningsDistribution": 0.0,
                "StockExDividendTradingDate": "",
                "CashEarningsDistribution": 6.0,
                "CashExDividendTradingDate": "2026-03-17",
            },
        ],
    }
    monkeypatch.setattr(finmind_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    rows = finmind_client.fetch_ex_dividend_schedule_for_range("2330", "2026-01-01", "2026-12-31")

    assert [r["ex_date"] for r in rows] == ["2026-03-17", "2026-06-11"]


def test_fetch_ex_dividend_schedule_for_range_returns_empty_on_failure_message(monkeypatch):
    payload = {"msg": "error", "data": []}
    monkeypatch.setattr(finmind_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    assert finmind_client.fetch_ex_dividend_schedule_for_range("2330", "2026-01-01", "2026-12-31") == []


def test_fetch_stock_name_returns_stock_name_field(monkeypatch):
    payload = {
        "msg": "success",
        "data": [{"industry_category": "光電業", "stock_id": "3595", "stock_name": "山太士", "type": "emerging", "date": "2026-08-16"}],
    }
    monkeypatch.setattr(finmind_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    assert finmind_client.fetch_stock_name("3595") == "山太士"


def test_fetch_stock_name_returns_empty_string_when_not_found(monkeypatch):
    payload = {"msg": "success", "data": []}
    monkeypatch.setattr(finmind_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    assert finmind_client.fetch_stock_name("9999999") == ""


def test_fetch_stock_name_returns_empty_string_on_failure_message(monkeypatch):
    payload = {"msg": "error", "data": []}
    monkeypatch.setattr(finmind_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    assert finmind_client.fetch_stock_name("3595") == ""
