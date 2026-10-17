from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from bhavcopy_fixture import csv_text, row
from tradio.data.nse import parse_bhavcopy_csv
from tradio.ingest import backfill, store_day
from tradio.data.base import CorporateAction
from tradio.models import DailyPrice, Instrument, PriceAdjustment, TradingDay

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
    def __init__(self, days, actions_published=True):
        self.days = days
        self.actions_published = actions_published
        self.requested = []
        self.actions_requested = []

    def fetch_day(self, day):
        self.requested.append(day)
        return bars(day, ("TCS", "100")) if day in self.days else None

    def fetch_actions(self, day):
        self.actions_requested.append(day)
        if not self.actions_published:
            return None
        return [CorporateAction("TCS", day, "bonus 1:1", Decimal("0.5"), "BONUS 1:1", "bc")] if day == WED else []

    def fetch_actions_between(self, start, end):
        self.api_range = (start, end)
        return [
            CorporateAction("TCS", WED, "bonus 1:1", Decimal("0.5"), "Bonus 1:1", "api"),  # duplicate of bc
            CorporateAction("INFY", MON, "split 10>2", Decimal("0.2"), "Split", "api"),  # bc missed it
            CorporateAction("INFY", WED + timedelta(days=5), "split 2>1", Decimal("0.5"), "Split", "api"),  # not ex yet
        ]


def test_backfill_stores_trading_days_and_skips_stored_ones(db):
    source = FakeSource({MON, WED})
    assert backfill(db, source, MON, WED, delay=0) == 2
    assert source.requested == [MON, TUE, WED]

    assert db.get(PriceAdjustment, ("TCS", WED, "bonus 1:1")).source == "bc"
    assert db.get(PriceAdjustment, ("INFY", MON, "split 10>2")).source == "api"
    assert db.scalar(select(func.count()).select_from(PriceAdjustment)) == 2
    assert source.api_range == (MON, WED)

    # Rerun only retries days that had no file.
    source.requested.clear()
    assert backfill(db, source, MON, WED, delay=0) == 2
    assert source.requested == [TUE]


def test_backfill_retries_actions_without_refetching_prices(db):
    source = FakeSource({MON}, actions_published=False)
    backfill(db, source, MON, MON, delay=0)
    assert db.get(TradingDay, MON).actions_loaded is False

    source.actions_published = True
    source.requested.clear()
    backfill(db, source, MON, MON, delay=0)
    assert source.requested == []
    assert source.actions_requested == [MON, MON]
    assert db.get(TradingDay, MON).actions_loaded is True
