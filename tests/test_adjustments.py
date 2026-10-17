from datetime import date
from decimal import Decimal

from sqlalchemy import select

from bhavcopy_fixture import csv_text, row
from tradio.adjustments import adjusted_closes, gap_checks, store_actions
from tradio.data.base import CorporateAction
from tradio.data.nse import parse_bhavcopy_csv
from tradio.ingest import store_day
from tradio.models import PriceAdjustment

D1, D2, D3, D4 = (date(2026, 10, d) for d in (12, 13, 14, 15))


def load(db, day, *rows):
    store_day(db, parse_bhavcopy_csv(csv_text(*(row(day, *r) for r in rows))))
    db.commit()


def action(symbol, ex_date, key="split 2>1", factor="0.5", source="bc"):
    return CorporateAction(symbol, ex_date, key, Decimal(factor) if factor else None, key, source)


def factors(db, symbol):
    return {(a.ex_date, a.action): a.factor for a in db.scalars(select(PriceAdjustment).where(PriceAdjustment.symbol == symbol))}


def test_split_is_applied_to_earlier_prices(db):
    load(db, D1, ("TCS", "100"))
    load(db, D2, ("TCS", "102"))
    load(db, D3, ("TCS", "52", "102"))  # real bhavcopy: prev close is NOT adjusted
    load(db, D4, ("TCS", "53"))
    store_actions(db, [action("TCS", D3)])

    closes = adjusted_closes(db, ["TCS"])["TCS"]
    assert closes == [(D1, Decimal("50")), (D2, Decimal("51")), (D3, Decimal("52")), (D4, Decimal("53"))]


def test_same_day_actions_multiply(db):
    # Bajaj Finance, 16 Jun 2025: bonus 4:1 and split Rs 2 -> Re 1 on one ex-date.
    load(db, D1, ("BAJFINANCE", "9331"))
    load(db, D2, ("BAJFINANCE", "956"))
    store_actions(db, [action("BAJFINANCE", D2, "bonus 4:1", "0.2"), action("BAJFINANCE", D2)])

    assert dict(adjusted_closes(db, ["BAJFINANCE"])["BAJFINANCE"])[D1] == Decimal("933.1")


def test_same_action_from_both_sources_is_stored_once(db):
    store_actions(db, [action("TCS", D2, source="bc")])
    store_actions(db, [action("TCS", D2, source="api")])
    db.commit()
    assert factors(db, "TCS") == {(D2, "split 2>1"): Decimal("0.5")}


def test_demerger_factor_is_the_ex_date_gap(db):
    load(db, D1, ("TMPV", "660"))
    load(db, D2, ("TMPV", "396"))  # fixture sets open = close
    store_actions(db, [action("TMPV", D2, "demerger", None)])
    assert factors(db, "TMPV") == {(D2, "demerger"): Decimal("0.6")}


def test_demerger_without_prices_is_skipped_for_now(db):
    assert store_actions(db, [action("TMPV", D2, "demerger", None)]) == 0
    assert factors(db, "TMPV") == {}


def test_actions_on_different_days_compound(db):
    load(db, D1, ("TCS", "400"))
    load(db, D2, ("TCS", "200"))
    load(db, D3, ("TCS", "100"))
    store_actions(db, [action("TCS", D2, "bonus 1:1"), action("TCS", D3)])

    closes = dict(adjusted_closes(db, ["TCS"])["TCS"])
    assert closes[D1] == Decimal("100")
    assert closes[D2] == Decimal("100")


def test_gap_check_combines_same_day_actions_and_skips_demergers(db):
    load(db, D1, ("BAJFINANCE", "1000"), ("TMPV", "100"))
    load(db, D2, ("BAJFINANCE", "102"), ("TMPV", "60"))
    store_actions(
        db,
        [
            action("BAJFINANCE", D2, "bonus 4:1", "0.2"),
            action("BAJFINANCE", D2),
            action("TMPV", D2, "demerger", None),
            action("GONE", D2),
        ],
    )

    checks = {c.symbol: c for c in gap_checks(db)}
    assert set(checks) == {"BAJFINANCE", "GONE"}
    assert checks["BAJFINANCE"].actions == "bonus 4:1 + split 2>1"
    assert round(checks["BAJFINANCE"].factor, 6) == Decimal("0.1")
    assert checks["BAJFINANCE"].gap == Decimal("0.102")
    assert checks["GONE"].gap is None
