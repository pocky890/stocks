import pytest

from stocks import twse_client


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


NEW_FLOWS_FIELDS = [
    "證券代號", "證券名稱",
    "外陸資買進股數(不含外資自營商)", "外陸資賣出股數(不含外資自營商)", "外陸資買賣超股數(不含外資自營商)",
    "外資自營商買進股數", "外資自營商賣出股數", "外資自營商買賣超股數",
    "投信買進股數", "投信賣出股數", "投信買賣超股數",
    "自營商買賣超股數",
    "自營商買進股數(自行買賣)", "自營商賣出股數(自行買賣)", "自營商買賣超股數(自行買賣)",
    "自營商買進股數(避險)", "自營商賣出股數(避險)", "自營商買賣超股數(避險)",
    "三大法人買賣超股數",
]
# 2018年之前的舊格式沒有把「外資」拆成兩欄，欄位數比新格式少3個，見
# fetch_institutional_flows_for_date docstring
OLD_FLOWS_FIELDS = [
    "證券代號", "證券名稱",
    "外資買進股數", "外資賣出股數", "外資買賣超股數",
    "投信買進股數", "投信賣出股數", "投信買賣超股數",
    "自營商買賣超股數",
    "自營商買進股數(自行買賣)", "自營商賣出股數(自行買賣)", "自營商買賣超股數(自行買賣)",
    "自營商買進股數(避險)", "自營商賣出股數(避險)", "自營商買賣超股數(避險)",
    "三大法人買賣超股數",
]


def test_fetch_institutional_flows_parses_foreign_trust_dealer_total(monkeypatch):
    payload = {
        "stat": "OK",
        "fields": NEW_FLOWS_FIELDS,
        "data": [
            ["2330", "台積電", "1000", "500", "500", "0", "0", "100", "0", "0", "50", "-30", "0", "0", "0", "0", "0", "0", "520"]
        ],
    }
    monkeypatch.setattr(twse_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    rows = twse_client.fetch_institutional_flows_for_date("2026-08-04")

    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "2330"
    assert row["foreign_net"] == 500 + 100, "新格式：外陸資買賣超(idx4) + 外資自營商買賣超(idx7)"
    assert row["trust_net"] == 50
    assert row["dealer_net"] == -30
    assert row["total_net"] == 520


def test_fetch_institutional_flows_parses_old_format_without_foreign_split(monkeypatch):
    # 2026-08-14拉長回測到10年時發現：2018年以前的T86沒有「外資自營商」這個獨立欄位，
    # 硬編位置的舊寫法碰到這種資料會直接IndexError，改成照欄位名稱查值後兩種格式都要能解析
    payload = {
        "stat": "OK",
        "fields": OLD_FLOWS_FIELDS,
        "data": [["2344", "華邦電", "16705000", "7303017", "9401983", "4427000", "13000", "4414000", "14170000", "0", "0", "0", "0", "0", "0", "27985983"]],
    }
    monkeypatch.setattr(twse_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    rows = twse_client.fetch_institutional_flows_for_date("2016-09-01")

    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "2344"
    assert row["foreign_net"] == 9401983, "舊格式：外資買賣超只有單一欄位，不用加總"
    assert row["trust_net"] == 4414000
    assert row["dealer_net"] == 14170000
    assert row["total_net"] == 27985983


def test_fetch_institutional_flows_returns_empty_on_bad_stat(monkeypatch):
    monkeypatch.setattr(
        twse_client.requests, "get", lambda *a, **k: FakeResponse({"stat": "很抱歉，沒有符合條件的資料!"})
    )
    assert twse_client.fetch_institutional_flows_for_date("2026-08-04") == []


def test_fetch_valuations_handles_dash_as_missing_pe(monkeypatch):
    payload = {
        "stat": "OK",
        "fields": ["證券代號", "證券名稱", "收盤價", "殖利率(%)", "股利年度", "本益比", "股價淨值比", "財報年/季"],
        "data": [["2330", "台積電", "600", "1.5", 114, "-", "10.2", "115/1"]],
    }
    monkeypatch.setattr(twse_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    rows = twse_client.fetch_valuations_for_date("2026-08-04")

    assert len(rows) == 1
    assert rows[0]["pe_ratio"] is None, "'-' from TWSE means no P/E (loss-making), not zero"
    assert rows[0]["dividend_yield"] == pytest.approx(1.5)
    assert rows[0]["pb_ratio"] == pytest.approx(10.2)


def test_fetch_valuations_parses_old_format_without_close_price_column(monkeypatch):
    # 2018年以前的舊格式只有[代號,名稱,本益比,殖利率,股價淨值比]5欄，沒有「收盤價」跟
    # 「股利年度」，本益比/股價淨值比的位置因此跟新格式不一樣，見
    # fetch_valuations_for_date docstring
    payload = {
        "stat": "OK",
        "fields": ["證券代號", "證券名稱", "本益比", "殖利率(%)", "股價淨值比"],
        "data": [["2330", "台積電", "15.2", "2.1", "3.8"]],
    }
    monkeypatch.setattr(twse_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    rows = twse_client.fetch_valuations_for_date("2016-09-01")

    assert len(rows) == 1
    assert rows[0]["pe_ratio"] == pytest.approx(15.2)
    assert rows[0]["dividend_yield"] == pytest.approx(2.1)
    assert rows[0]["pb_ratio"] == pytest.approx(3.8)


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
    payload = [
        {"公司代號": "2330", "公司名稱": "台灣積體電路製造股份有限公司", "公司簡稱": "台積電", "產業別": "24"}
    ]
    monkeypatch.setattr(twse_client.requests, "get", lambda *a, **k: FakeResponse(payload))

    rows = twse_client.fetch_company_directory()

    assert rows == [{"symbol": "2330", "name": "台積電", "industry_code": "24"}]
