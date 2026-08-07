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
