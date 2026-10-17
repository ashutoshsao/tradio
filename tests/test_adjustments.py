from datetime import date
from decimal import Decimal

from bhavcopy_fixture import csv_text, row
from tradio.adjustments import adjusted_closes, detect_adjustments
from tradio.data.nse import parse_bhavcopy_csv
from tradio.ingest import store_day
from tradio.models import PriceAdjustment

D1, D2, D3, D4 = (date(2026, 10, d) for d in (12, 13, 14, 15))


def load(db, day, *rows):
    store_day(db, parse_bhavcopy_csv(csv_text(*(row(day, *r) for r in rows))))
    db.commit()


def test_split_is_detected_and_applied(db):
    load(db, D1, ("TCS", "100", "99"))
    load(db, D2, ("TCS", "102", "100"))
    load(db, D3, ("TCS", "52", "51"))  # 1:2 split: NSE reports prev close 102 -> 51
    load(db, D4, ("TCS", "53", "52"))

    assert detect_adjustments(db) == 1
    db.commit()
    assert db.get(PriceAdjustment, ("TCS", D3)).factor == Decimal("0.5")

    closes = adjusted_closes(db, ["TCS"])["TCS"]
    assert closes == [(D1, Decimal("50")), (D2, Decimal("51")), (D3, Decimal("52")), (D4, Decimal("53"))]


def test_detection_is_idempotent(db):
    load(db, D1, ("TCS", "100", "100"))
    load(db, D2, ("TCS", "50", "50"))
    assert detect_adjustments(db) == 1
    assert detect_adjustments(db) == 0


def test_normal_move_is_not_an_adjustment(db):
    load(db, D1, ("TCS", "100", "100"))
    load(db, D2, ("TCS", "80", "100"))  # 20% fall, but prev close matches
    assert detect_adjustments(db) == 0


def test_gap_in_a_stock_is_not_an_adjustment(db):
    load(db, D1, ("TCS", "100", "100"), ("INFY", "10", "10"))
    load(db, D2, ("INFY", "10", "10"))  # TCS missing (e.g. moved to BE series)
    load(db, D3, ("TCS", "90", "95"), ("INFY", "10", "10"))  # prev close is the BE close we never saw
    assert detect_adjustments(db) == 0


def test_two_actions_compound(db):
    load(db, D1, ("TCS", "400", "400"))
    load(db, D2, ("TCS", "200", "200"))  # 1:1 bonus
    load(db, D3, ("TCS", "100", "100"))  # 1:2 split
    detect_adjustments(db)

    closes = dict(adjusted_closes(db, ["TCS"])["TCS"])
    assert closes[D1] == Decimal("100")
    assert closes[D2] == Decimal("100")
