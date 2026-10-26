import base64
import json
from datetime import date, datetime
from decimal import Decimal

import httpx
import pytest

from tradio.config import IST
from tradio.data.upstox import BASE_URL, Quote, UpstoxError, UpstoxSource, token_expiry


def source(handler) -> tuple[UpstoxSource, list[httpx.Request]]:
    seen = []

    def record(request):
        seen.append(request)
        return handler(request)

    client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(record))
    return UpstoxSource("token", client=client, backoff=0), seen


def ok(data) -> httpx.Response:
    return httpx.Response(200, json={"status": "success", "data": data})


def test_quotes_are_batched_and_keyed_by_isin():
    def handler(request):
        keys = request.url.params["instrument_key"].split(",")
        return ok(
            {
                f"NSE_EQ:S{i}": {"last_price": 101.5, "instrument_token": k, "ltq": 1, "volume": 10, "cp": 100.0}
                for i, k in enumerate(keys)
            }
        )

    upstox, seen = source(handler)
    quotes = upstox.quotes([f"INE{i:07d}01" for i in range(1203)])
    assert len(seen) == 3  # 500 + 500 + 203
    assert len(quotes) == 1203
    assert quotes["INE000000701"] == Quote(Decimal("101.5"), Decimal("100.0"))


def test_market_hours_and_holidays_are_cached():
    def handler(request):
        if request.url.path.endswith("2026-10-18"):
            return ok([{"exchange": "MCX", "start_time": 1, "end_time": 2}])
        return ok([{"exchange": "NSE", "start_time": 1792122300000, "end_time": 1792144800000}])

    upstox, seen = source(handler)
    hours = upstox.market_hours(date(2026, 10, 16))
    assert (hours.start, hours.end) == (datetime(2026, 10, 16, 9, 15, tzinfo=IST), datetime(2026, 10, 16, 15, 30, tzinfo=IST))
    assert upstox.market_hours(date(2026, 10, 18)) is None  # Sunday: only MCX listed
    upstox.market_hours(date(2026, 10, 16))
    assert len(seen) == 2


def test_auth_errors_fail_fast_and_server_errors_retry():
    upstox, seen = source(lambda r: httpx.Response(401, json={"status": "error", "errors": [{"errorCode": "UDAPI100050"}]}))
    with pytest.raises(UpstoxError, match="UDAPI100050"):
        upstox.quotes(["INE000000001"])
    assert len(seen) == 1

    upstox, seen = source(lambda r: httpx.Response(503))
    with pytest.raises(UpstoxError):
        upstox.quotes(["INE000000001"])
    assert len(seen) == 3


def test_token_expiry_reads_the_jwt_claim():
    claims = base64.urlsafe_b64encode(json.dumps({"exp": 1823896800}).encode()).decode().rstrip("=")
    assert token_expiry(f"header.{claims}.signature") == datetime(2027, 10, 19, 3, 30, tzinfo=IST)
    assert token_expiry("not-a-jwt") is None


def test_candles_use_v3_15_minute_endpoints_and_sort_deduplicate():
    rows = [
        ["2026-10-23T09:30:00+05:30", 1, 2, 1, 120, 10, 0],
        ["2026-10-23T09:15:00+05:30", 1, 2, 1, 100.25, 10, 0],
        ["2026-10-23T09:30:00+05:30", 1, 2, 1, 120, 10, 0],
    ]
    upstox, seen = source(lambda request: ok({"candles": rows}))
    candles = upstox.candles("INE002A01018", date(2026, 10, 23))
    assert len(candles) == 2
    assert candles[0].close == Decimal("100.25")
    assert candles[0].start == datetime(2026, 10, 23, 9, 15, tzinfo=IST)
    assert seen[0].url.path == "/v3/historical-candle/intraday/NSE_EQ|INE002A01018/minutes/15"
    assert "%7C" in str(seen[0].url)
    upstox.candles("INE002A01018", date(2026, 10, 23), date(2026, 10, 1))
    assert seen[1].url.path == "/v3/historical-candle/NSE_EQ|INE002A01018/minutes/15/2026-10-23/2026-10-01"


def test_candles_reject_missing_timezone():
    upstox, _ = source(lambda request: ok({"candles": [["2026-10-23T09:15:00", 1, 2, 1, 100, 10, 0]]}))
    with pytest.raises(UpstoxError, match="timezone"):
        upstox.candles("INE002A01018", date(2026, 10, 23))
