from datetime import date
from decimal import Decimal

from sqlalchemy import func, select

from bhavcopy_fixture import csv_text, row
from tradio.data.nse import parse_bhavcopy_csv
from tradio.ingest import backfill, store_day
from tradio.models import DailyPrice, Instrument

MON, TUE, WED = date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)


def bars(day, *symbols_and_closes):
    return parse_bhavcopy_csv(csv_text(*(row(day, s, c) for s, c in symbols_and_closes)))


def test_store_day_is_idempotent(db):
    store_day(db, bars(MON, ("TCS", "100"), ("INFY", "200")))
    store_day(db, bars(MON, ("TCS", "101"), ("INFY", "200")))
    db.commit()

    assert db.scalar(select(func.count()).select_from(DailyPrice)) == 2
    assert db.get(DailyPrice, ("TCS", MON)).close == Decimal("101")


def test_older_day_does_not_overwrite_newer_instrument_details(db):
    store_day(db, bars(TUE, ("TCS", "100")))
    store_day(db, bars(MON, ("TCS", "100")))
    db.commit()

    assert db.get(Instrument, "TCS").last_seen_date == TUE


class FakeSource:
    def __init__(self, days):
        self.days = days
        self.requested = []

    def fetch_day(self, day):
        self.requested.append(day)
        return bars(day, ("TCS", "100")) if day in self.days else None


def test_backfill_stores_trading_days_and_skips_stored_ones(db):
    source = FakeSource({MON, WED})
    assert backfill(db, source, MON, WED, delay=0) == 2
    assert source.requested == [MON, TUE, WED]

    # Rerun only retries days that had no file.
    source.requested.clear()
    assert backfill(db, source, MON, WED, delay=0) == 0
    assert source.requested == [TUE]
