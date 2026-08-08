import pytest

from stocks import twse_client


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_fetch_institutional_flows_parses_foreign_trust_dealer_total(monkeypatch):
    payload = {
        "stat": "OK",
        "fields": ["證券代號", "證券名稱"] + [""] * 17,
        "data": [
            # idx: 0=code 1=name 2=fdi_buy 3=fdi_sell 4=fdi_net 5=fdid_buy 6=fdid_sell 7=fdid_net
            # 8=trust_buy 9=trust_sell 10=trust_net 11=dealer_net 12-17=dealer detail 18=total_net
            ["2330", "台積電", "1000", "500", "500", "0", "0", "100", "0", "0", "50", "-30", "0", "0", "0", "0", "0", "0", "520"]
        ],
    }
    monkeypatch.setattr(twse_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    rows = twse_client.fetch_institutional_flows_for_date("2026-08-04")

    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "2330"
    assert row["foreign_net"] == 500 + 100  # idx4 + idx7
    assert row["trust_net"] == 50
    assert row["dealer_net"] == -30
    assert row["total_net"] == 520


def test_fetch_institutional_flows_returns_empty_on_bad_stat(monkeypatch):
    monkeypatch.setattr(
        twse_client.requests, "get", lambda *a, **k: FakeResponse({"stat": "很抱歉，沒有符合條件的資料!"})
    )
    assert twse_client.fetch_institutional_flows_for_date("2026-08-04") == []


def test_fetch_margin_balances_uses_second_table(monkeypatch):
    payload = {
        "stat": "OK",
        "tables": [
            {"title": "市場統計", "fields": [], "data": [["irrelevant"]]},
            {
                "title": "融資融券彙總",
                "fields": [],
                "data": [
                    # idx: 0=symbol 1=name 2=margin_buy 3=margin_sell ... 6=margin_balance ...
                    # 8=short_buy 9=short_sell ... 12=short_balance
                    ["2330", "台積電", "100", "50", "0", "0", "9000", "0", "10", "5", "0", "0", "20", "0", "0", ""]
                ],
            },
        ],
    }
    monkeypatch.setattr(twse_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    rows = twse_client.fetch_margin_balances_for_date("2026-08-04")

    assert len(rows) == 1
    row = rows[0]
    assert row["margin_buy"] == 100
    assert row["margin_sell"] == 50
    assert row["margin_balance"] == 9000
    assert row["short_buy"] == 10
    assert row["short_sell"] == 5
    assert row["short_balance"] == 20


def test_fetch_valuations_handles_dash_as_missing_pe(monkeypatch):
    payload = {
        "stat": "OK",
        "data": [["2330", "台積電", "600", "1.5", 114, "-", "10.2", "115/1"]],
    }
    monkeypatch.setattr(twse_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    rows = twse_client.fetch_valuations_for_date("2026-08-04")

    assert len(rows) == 1
    assert rows[0]["pe_ratio"] is None, "'-' from TWSE means no P/E (loss-making), not zero"
    assert rows[0]["dividend_yield"] == pytest.approx(1.5)
    assert rows[0]["pb_ratio"] == pytest.approx(10.2)


def test_fetch_ex_dividend_schedule_converts_roc_date(monkeypatch):
    payload = [
        {
            "Date": "1150805",
            "Code": "2330",
            "Name": "台積電",
            "Exdividend": "息",
            "StockDividendRatio": "",
            "CashDividend": "2.750000",
        }
    ]
    monkeypatch.setattr(twse_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    rows = twse_client.fetch_ex_dividend_schedule()

    assert rows[0]["ex_date"] == "2026-08-05"
    assert rows[0]["cash_dividend"] == pytest.approx(2.75)
    assert rows[0]["symbol"] == "2330"


def test_fetch_company_directory_uses_short_name_not_legal_name(monkeypatch):
    payload = [{"公司代號": "2330", "公司名稱": "台灣積體電路製造股份有限公司", "公司簡稱": "台積電"}]
    monkeypatch.setattr(twse_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    rows = twse_client.fetch_company_directory()

    assert rows == [{"symbol": "2330", "name": "台積電"}]
