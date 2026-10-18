"""Upstox market data with a read-only Analytics Token. See docs/v2-spec.md §2.

Only GET market-data endpoints are used. Personal and order endpoints are
refused for this token anyway (no static IP is registered).
"""

import base64
import binascii
import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

import httpx

from tradio.config import IST

log = logging.getLogger(__name__)

BASE_URL = "https://api.upstox.com"
MAX_KEYS = 500  # instrument keys per quote request


class UpstoxError(Exception):
    pass


@dataclass(frozen=True)
class Quote:
    price: Decimal  # last traded price
    prev_close: Decimal  # NSE's official previous close, already adjusted on an ex-date


@dataclass(frozen=True)
class MarketHours:
    start: datetime
    end: datetime


class LivePriceSource(Protocol):
    def quotes(self, isins: list[str]) -> dict[str, Quote]:
        """Live quotes by ISIN. Stocks Upstox doesn't know are left out."""
        ...

    def market_hours(self, day: date) -> MarketHours | None:
        """NSE equity session on `day`, or None if it's closed."""
        ...


class UpstoxSource:
    def __init__(self, token: str, client: httpx.Client | None = None, retries: int = 3, backoff: float = 2.0):
        self.client = client or httpx.Client(
            base_url=BASE_URL,
            headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
            timeout=30,
        )
        self.retries = retries
        self.backoff = backoff
        self._hours: dict[date, MarketHours | None] = {}

    def quotes(self, isins: list[str]) -> dict[str, Quote]:
        result = {}
        for i in range(0, len(isins), MAX_KEYS):
            keys = ",".join(f"NSE_EQ|{isin}" for isin in isins[i : i + MAX_KEYS])
            for q in self._get("/v3/market-quote/ltp", {"instrument_key": keys}).values():
                isin = q["instrument_token"].split("|", 1)[1]
                result[isin] = Quote(Decimal(str(q["last_price"])), Decimal(str(q["cp"])))
        return result

    def market_hours(self, day: date) -> MarketHours | None:
        # Timings for a date don't change, so each is fetched once per process.
        if day not in self._hours:
            nse = [x for x in self._get(f"/v2/market/timings/{day:%Y-%m-%d}") if x["exchange"] == "NSE"]
            self._hours[day] = (
                MarketHours(_from_ms(nse[0]["start_time"]), _from_ms(nse[0]["end_time"])) if nse else None
            )
        return self._hours[day]

    def _get(self, path: str, params: dict | None = None):
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.client.get(path, params=params)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
                body = resp.json()
                if resp.status_code != 200 or body.get("status") != "success":
                    codes = [e.get("errorCode") for e in body.get("errors") or []]
                    raise UpstoxError(f"{path}: HTTP {resp.status_code} {codes}")
                return body["data"]
            except (httpx.HTTPError, ValueError) as exc:
                if attempt == self.retries:
                    raise UpstoxError(f"{path}: {exc}") from exc
                log.warning("upstox %s failed (%s), retry %d/%d", path, exc, attempt, self.retries)
                time.sleep(self.backoff * attempt)
        raise AssertionError("unreachable")


def _from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, IST)


def token_expiry(token: str) -> datetime | None:
    """The token's `exp` claim (it's a JWT). Read without verifying: it only drives a reminder."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return datetime.fromtimestamp(claims["exp"], IST)
    except (IndexError, KeyError, TypeError, ValueError, binascii.Error):
        return None
