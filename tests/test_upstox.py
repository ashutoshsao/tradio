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
