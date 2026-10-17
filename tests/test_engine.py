from datetime import date, timedelta

from sqlalchemy import func, select

from bhavcopy_fixture import csv_text, row
from tradio.data.nse import parse_bhavcopy_csv
from tradio.engine import evaluate
from tradio.ingest import store_day
from tradio.models import Direction, SignalEvent, SignalState, TradingDay

START = date(2026, 1, 1)


def load_series(db, symbol, closes):
    for i, close in enumerate(closes):
        day = START + timedelta(days=i)
        store_day(db, parse_bhavcopy_csv(csv_text(row(day, symbol, str(close)))))
        db.merge(TradingDay(trade_date=day, actions_loaded=True))
    db.commit()


def test_evaluate_records_states_and_events_once(db):
    load_series(db, "TCS", [100] * 25 + [110] * 5)

    first = evaluate(db, ["TCS"])
    db.commit()
    assert len(first) == 1
    assert db.scalar(select(func.count()).select_from(SignalState)) == 10  # days 21..30
    event = db.get(SignalEvent, first[0])
    assert (event.symbol, event.direction, event.trade_date) == ("TCS", Direction.BULLISH, START + timedelta(days=25))

    assert evaluate(db, ["TCS"]) == []  # rerun adds nothing


def test_evaluate_skips_stocks_without_enough_history(db):
    load_series(db, "NEW", [100] * 10)
    assert evaluate(db, ["NEW"]) == []
