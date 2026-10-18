from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class Bar:
    """One stock's prices for one trading day, as reported (unadjusted)."""

    symbol: str
    isin: str
    name: str
    series: str
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    prev_close: Decimal
    volume: int


@dataclass(frozen=True)
class CorporateAction:
    """A split, bonus, consolidation or demerger. Prices before ex_date get multiplied by factor.

    `action` is a normalised key so the same action from two sources matches.
    `factor` is None for demergers: there's no ratio, so it comes from the ex-date price gap.
    """

    symbol: str
    ex_date: date
    action: str
    factor: Decimal | None
    purpose: str
    source: str


class MarketDataSource(Protocol):
    def fetch_day(self, day: date) -> list[Bar] | None:
        """All EQ bars for `day`, or None if there was no trading that day (or not yet published)."""
        ...

    def fetch_actions(self, day: date) -> list[CorporateAction] | None:
        """Actions going ex on or before `day`, from that day's files. None if unpublished."""
        ...

    def fetch_actions_between(self, start: date, end: date) -> list[CorporateAction] | None:
        """Actions with ex-dates in [start, end] from a range query. None if unavailable."""
        ...
