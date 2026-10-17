"""Load bhavcopy bars into the database."""

import logging
import time
from collections.abc import Iterator
from dataclasses import asdict
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from tradio.data.base import Bar, MarketDataSource
from tradio.models import DailyPrice, Instrument

log = logging.getLogger(__name__)


def store_day(session: Session, bars: list[Bar]) -> None:
    """Upsert one day's bars. Safe to call again for the same day."""
    if not bars:
        return
    # ON CONFLICT can't touch the same row twice in one statement.
    bars = list({b.symbol: b for b in bars}.values())

    instruments = insert(Instrument).values(
        [
            {"symbol": b.symbol, "isin": b.isin, "name": b.name, "series": b.series, "last_seen_date": b.trade_date}
            for b in bars
        ]
    )
    session.execute(
        instruments.on_conflict_do_update(
            index_elements=[Instrument.symbol],
            set_={
                "isin": instruments.excluded.isin,
                "name": instruments.excluded.name,
                "series": instruments.excluded.series,
                "last_seen_date": instruments.excluded.last_seen_date,
            },
            # Backfilling older days must not overwrite newer instrument details.
            where=Instrument.last_seen_date <= instruments.excluded.last_seen_date,
        )
    )

    prices = insert(DailyPrice).values(
        [{k: v for k, v in asdict(b).items() if k not in ("isin", "name", "series")} for b in bars]
    )
    session.execute(
        prices.on_conflict_do_update(
            index_elements=[DailyPrice.symbol, DailyPrice.trade_date],
            set_={c: prices.excluded[c] for c in ("open", "high", "low", "close", "prev_close", "volume")},
        )
    )


def stored_dates(session: Session, start: date, end: date) -> set[date]:
    return set(
        session.scalars(
            select(DailyPrice.trade_date).where(DailyPrice.trade_date.between(start, end)).distinct()
        )
    )


def _days(start: date, end: date) -> Iterator[date]:
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def backfill(session: Session, source: MarketDataSource, start: date, end: date, delay: float = 1.0) -> int:
    """Fetch and store every trading day in [start, end] not already stored.

    Every calendar day is tried: a missing file means no trading, and weekend
    special sessions (Muhurat, budget day) are picked up without a holiday calendar.
    Returns the number of trading days stored.
    """
    have = stored_dates(session, start, end)
    stored = 0
    for day in _days(start, end):
        if day in have:
            continue
        bars = source.fetch_day(day)
        if bars is None:
            log.info("%s: no bhavcopy", day)
        else:
            store_day(session, bars)
            session.commit()
            stored += 1
            log.info("%s: stored %d bars", day, len(bars))
        if delay:
            time.sleep(delay)
    return stored
