from datetime import date
from decimal import Decimal

import httpx
import pytest

from bhavcopy_fixture import csv_text, row, zip_bytes
from tradio.data.nse import NseBhavcopySource, parse_bhavcopy_csv

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


def _source(handler) -> NseBhavcopySource:
    return NseBhavcopySource(client=httpx.Client(transport=httpx.MockTransport(handler)), backoff=0)


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
