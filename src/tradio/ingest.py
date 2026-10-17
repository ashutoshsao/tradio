"""Load bhavcopy bars into the database."""

import logging
import time
from collections.abc import Iterator
from dataclasses import asdict
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from tradio.adjustments import store_actions
from tradio.data.base import Bar, MarketDataSource
from tradio.models import DailyPrice, Instrument, TradingDay

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


def _days(start: date, end: date) -> Iterator[date]:
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def ingest_day(session: Session, source: MarketDataSource, day: date) -> bool:
    """Load prices and corporate actions for `day` if not loaded yet. Returns False if no trading."""
    trading_day = session.get(TradingDay, day)
    if trading_day is None:
        bars = source.fetch_day(day)
        if bars is None:
            return False
        store_day(session, bars)
        trading_day = TradingDay(trade_date=day)
        session.add(trading_day)
        session.flush()
        log.info("%s: stored %d bars", day, len(bars))

    if not trading_day.actions_loaded:
        actions = source.fetch_actions(day)
        if actions is not None:
            store_actions(session, actions)
            trading_day.actions_loaded = True
    session.commit()
    return True


def backfill(session: Session, source: MarketDataSource, start: date, end: date, delay: float = 1.0) -> int:
    """Load every trading day in [start, end] that isn't fully loaded yet, then
    cross-fill corporate actions for the whole range from NSE's actions API.

    Every calendar day is tried: a missing bhavcopy means no trading, and weekend
    special sessions (Muhurat, budget day) are picked up without a holiday calendar.
    Returns the number of trading days seen.
    """
    done = set(
        session.scalars(
            select(TradingDay.trade_date).where(
                TradingDay.trade_date.between(start, end), TradingDay.actions_loaded
            )
        )
    )
    seen = len(done)
    for day in _days(start, end):
        if day in done:
            continue
        if ingest_day(session, source, day):
            seen += 1
        else:
            log.info("%s: no bhavcopy", day)
        if delay:
            time.sleep(delay)

    sync_actions_api(session, source, start, end)
    return seen


def sync_actions_api(session: Session, source: MarketDataSource, start: date, end: date) -> None:
    """Add actions the daily bc files miss. Idempotent, so overlapping ranges are fine."""
    latest = session.scalar(select(func.max(TradingDay.trade_date)).where(TradingDay.trade_date <= end))
    if latest is None:
        return
    actions = source.fetch_actions_between(start, latest)
    if actions is None:
        return  # logged by the source; the next run's range covers it again
    # Only actions already ex: an announced one may still be rescheduled.
    added = store_actions(session, [a for a in actions if a.ex_date <= latest])
    session.commit()
    log.info("actions API %s..%s: %d share-change actions", start, latest, added)
