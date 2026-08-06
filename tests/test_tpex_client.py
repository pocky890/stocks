import pytest

from stocks import tpex_client


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_fetch_institutional_flows_latest_parses_foreign_trust_dealer_total(monkeypatch):
    payload = [
        {
            "Date": "1150805",
            "SecuritiesCompanyCode": "8299",
            "CompanyName": "群聯",
            "ForeignInvestorsInclude MainlandAreaInvestors-Difference": "-275780",
            "SecuritiesInvestmentTrustCompanies-Difference": "20325",
            "Dealers-Difference": "-269444",
            "TotalDifference": "-524899",
        }
    ]
    monkeypatch.setattr(tpex_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    rows = tpex_client.fetch_institutional_flows_latest()

    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "8299"
    assert row["date"] == "2026-08-05"
    assert row["foreign_net"] == -275780
    assert row["trust_net"] == 20325
    assert row["dealer_net"] == -269444
    assert row["total_net"] == -524899


def test_fetch_margin_balances_latest_maps_covering_and_short_sale(monkeypatch):
    payload = [
        {
            "Date": "1150804",
            "SecuritiesCompanyCode": "8299",
            "MarginPurchase": "82",
            "MarginSales": "167",
            "MarginPurchaseBalance": "3871",
            "ShortConvering": "3",
            "ShortSale": "0",
            "ShortSaleBalance": "10",
        }
    ]
    monkeypatch.setattr(tpex_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    rows = tpex_client.fetch_margin_balances_latest()

    assert rows[0]["margin_buy"] == 82
    assert rows[0]["margin_sell"] == 167
    assert rows[0]["margin_balance"] == 3871
    assert rows[0]["short_buy"] == 3, "ShortConvering (回補) maps to short_buy, matching TWSE semantics"
    assert rows[0]["short_sell"] == 0, "ShortSale (放空) maps to short_sell"
    assert rows[0]["short_balance"] == 10


def test_fetch_valuations_latest_parses_pe_yield_pb(monkeypatch):
    payload = [
        {
            "Date": "1150805",
            "SecuritiesCompanyCode": "1240",
            "CompanyName": "茂生農經",
            "PriceEarningRatio": "11.80",
            "YieldRatio": "6.35",
            "PriceBookRatio": "1.56",
        }
    ]
    monkeypatch.setattr(tpex_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    rows = tpex_client.fetch_valuations_latest()

    assert rows[0]["pe_ratio"] == pytest.approx(11.80)
    assert rows[0]["dividend_yield"] == pytest.approx(6.35)
    assert rows[0]["pb_ratio"] == pytest.approx(1.56)


def test_fetch_ex_dividend_schedule_converts_roc_date(monkeypatch):
    payload = [
        {
            "ExRrightsExDividendDate": "1150727",
            "SecuritiesCompanyCode": "00858",
            "ExRrightsExDividend": "除息",
            "StockDividendRatio": "0.00000000",
            "CashDividend": "1.40000000",
        }
    ]
    monkeypatch.setattr(tpex_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    rows = tpex_client.fetch_ex_dividend_schedule()

    assert rows[0]["symbol"] == "00858"
    assert rows[0]["ex_date"] == "2026-07-27"
    assert rows[0]["cash_dividend"] == pytest.approx(1.40)
    assert rows[0]["detail"] == "除息"
