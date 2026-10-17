from datetime import date
from decimal import Decimal

import httpx
import pytest

from bhavcopy_fixture import csv_text, row, zip_bytes
from tradio.data.nse import NseArchiveSource, parse_actions_api, parse_bc_csv, parse_bhavcopy_csv, parse_purpose

DAY = date(2026, 10, 16)


def test_parse_keeps_only_eq_series():
    bars = parse_bhavcopy_csv(csv_text(row(DAY, "RELIANCE", "1211.20", "1215.00"), row(DAY, "XYZ", "10", series="BE")))

    assert [b.symbol for b in bars] == ["RELIANCE"]
    bar = bars[0]
    assert bar.trade_date == DAY
    assert bar.close == Decimal("1211.20")
    assert bar.prev_close == Decimal("1215.00")
    assert bar.volume == 1000


def test_parse_skips_malformed_rows():
    bad = row(DAY, "BAD", "1").replace(",1,1,1,1,1,", ",-,-,-,-,-,")
    assert [b.symbol for b in parse_bhavcopy_csv(csv_text(row(DAY, "OK", "5"), bad))] == ["OK"]


def _source(handler) -> NseArchiveSource:
    return NseArchiveSource(client=httpx.Client(transport=httpx.MockTransport(handler)), backoff=0)


def test_fetch_day_downloads_and_parses():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("BhavCopy_NSE_CM_0_0_0_20261016_F_0000.csv.zip")
        return httpx.Response(200, content=zip_bytes(csv_text(row(DAY, "TCS", "2465.00")), DAY))

    assert [b.symbol for b in _source(handler).fetch_day(DAY)] == ["TCS"]


def test_fetch_day_returns_none_when_no_file():
    assert _source(lambda r: httpx.Response(404)).fetch_day(DAY) is None


def test_fetch_day_retries_server_errors():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(200, content=zip_bytes(csv_text(row(DAY, "TCS", "1")), DAY))

    assert _source(handler).fetch_day(DAY) is not None
    assert len(calls) == 3


def test_fetch_day_raises_after_retries():
    with pytest.raises(httpx.HTTPStatusError):
        _source(lambda r: httpx.Response(503)).fetch_day(DAY)


@pytest.mark.parametrize(
    "purpose, expected",
    [
        ("BONUS 1:1", [("bonus 1:1", "0.5")]),
        ("BONUS 4:1", [("bonus 4:1", "0.2")]),  # 4 new shares for every 1 held
        ("Bonus 1:2", [("bonus 1:2", "0.6666666666666666666666666667")]),
        ("FV SPLT FRM RS 2 TO RE 1", [("split 2>1", "0.5")]),  # older bc wording
        ("FVSPLT FRM RS 10 TO RS 2", [("split 10>2", "0.2")]),  # current bc wording
        ("FV SPLT FRM RS 10 TO 1", [("split 10>1", "0.1")]),  # seen once, currency missing
        ("FVSPLT FRMRS 100 TO RE 1", [("split 100>1", "0.01")]),
        (
            "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share",  # API wording
            [("split 10>1", "0.1")],
        ),
        ("Consolidation Of Equity Shares From Re 1 Per Share To Rs 10 Per Share", [("consolidation 1>10", "10")]),
        ("DEMERGER", [("demerger", None)]),
        ("Demerger", [("demerger", None)]),
        ("DIV - RS 3 PER SH", []),
        ("RIGHTS 5:14@ PRM RS 72/-", []),
        ("Scheme Of Arrangement - Bonus Ncrps 4:1", []),
    ],
)
def test_parse_purpose(purpose, expected):
    assert parse_purpose(purpose) == [(k, Decimal(f) if f else None) for k, f in expected]


def test_same_action_gets_same_key_from_both_sources():
    (bc,) = parse_purpose("FVSPLT FRM RS 10 TO RS 2")
    (api,) = parse_purpose("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share")
    assert bc == api


def test_parse_actions_api():
    rows = [
        {"symbol": "SHRIRAMFIN", "series": "EQ", "exDate": "10-Jan-2025",
         "subject": "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share"},
        {"symbol": "IGL", "series": "EQ", "exDate": "01-Oct-2026", "subject": "Dividend - Rs 1.50 Per Share"},
        {"symbol": "XYZ", "series": "BE", "exDate": "01-Oct-2026", "subject": "Bonus 1:1"},
    ]
    (action,) = parse_actions_api(rows)
    assert (action.symbol, action.ex_date, action.action, action.factor, action.source) == (
        "SHRIRAMFIN", date(2025, 1, 10), "split 10>2", Decimal("0.2"), "api"
    )


def test_fetch_actions_between_returns_none_when_blocked():
    assert _source(lambda r: httpx.Response(403)).fetch_actions_between(DAY, DAY) is None


BC_HEADER = "SERIES,SYMBOL,SECURITY,RECORD_DT,BC_STRT_DT,BC_END_DT,EX_DT,ND_STRT_DT,ND_END_DT,PURPOSE"


def test_parse_bc_keeps_eq_share_changes_already_ex():
    text = "\n".join(
        [
            BC_HEADER,
            "EQ,BAJFINANCE,Bajaj Finance Limited,16/06/2025, , ,16/06/2025, , ,BONUS 4:1                ",
            "BE,BAJFINANCE,Bajaj Finance Limited,16/06/2025, , ,16/06/2025, , ,BONUS 4:1                ",
            "EQ,BAJFINANCE,Bajaj Finance Limited,16/06/2025, , ,16/06/2025, , ,FV SPLT FRM RS 2 TO RE 1 ",
            "EQ,IGL,Indraprastha Gas,16/06/2025, , ,16/06/2025, , ,DIV - RS 3 PER SH",
            "EQ,LATER,Later Ltd,20/06/2025, , ,20/06/2025, , ,BONUS 1:1",  # upcoming: ignored
        ]
    )
    actions = parse_bc_csv(text, date(2025, 6, 16))
    assert sorted((a.symbol, a.action, a.factor) for a in actions) == [
        ("BAJFINANCE", "bonus 4:1", Decimal("0.2")),
        ("BAJFINANCE", "split 2>1", Decimal("0.5")),
    ]


def test_parse_bc_accepts_iso_dates():
    text = BC_HEADER + "\nEQ,MOLDTKPAC,Mold-Tek Packaging Ltd,2026-10-09,,,2026-10-09,,,BONUS 1:1\n"
    (action,) = parse_bc_csv(text, date(2026, 10, 9))
    assert (action.symbol, action.ex_date, action.factor) == ("MOLDTKPAC", date(2026, 10, 9), Decimal("0.5"))
