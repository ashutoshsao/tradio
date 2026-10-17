from datetime import date, timedelta
from decimal import Decimal

from tradio.models import Direction, SmaState
from tradio.signals import classify, compute_events, compute_states

START = date(2026, 1, 1)


def series(*values) -> list[tuple[date, Decimal]]:
    return [(START + timedelta(days=i), Decimal(v)) for i, v in enumerate(values)]


def test_classify():
    assert classify(Decimal(3), Decimal(2), Decimal(1)) == SmaState.ABOVE
    assert classify(Decimal(1), Decimal(2), Decimal(3)) == SmaState.BELOW
    assert classify(Decimal(2), Decimal(1), Decimal(3)) == SmaState.MIXED
    # Ties are not a cross.
    assert classify(Decimal(2), Decimal(2), Decimal(1)) == SmaState.MIXED


def test_needs_21_closes():
    assert compute_states(series(*[100] * 20)) == []
    points = compute_states(series(*[100] * 21))
    assert len(points) == 1
    assert points[0].trade_date == START + timedelta(days=20)
    assert points[0].sma8 == points[0].sma21 == Decimal(100)


def test_sma_values():
    (point,) = compute_states(series(*range(1, 22)))  # closes 1..21
    assert point.sma8 == Decimal("17.5")  # mean(14..21)
    assert point.sma13 == Decimal(15)  # mean(9..21)
    assert point.sma21 == Decimal(11)  # mean(1..21)
    assert point.state == SmaState.ABOVE


def test_flat_then_rally_fires_one_bullish_event():
    points = compute_states(series(*[100] * 25, *[110] * 10))
    events = compute_events(points)

    assert [e.direction for e in events] == [Direction.BULLISH]
    # The first rally day lifts SMA8 above both slower averages straight away.
    assert events[0].point.trade_date == START + timedelta(days=25)


def test_crossing_one_average_at_a_time_fires_when_second_is_crossed():
    # Falling from the start, so the first point is already BELOW and doesn't fire.
    points = compute_states(series(*range(100, 70, -1), *range(70, 110)))
    events = compute_events(points)

    assert points[0].state == SmaState.BELOW
    assert [e.direction for e in events] == [Direction.BULLISH]
    bullish_day = events[0].point.trade_date
    by_day = {p.trade_date: p for p in points}
    # The day before the alert SMA8 had crossed only one average (MIXED), not both.
    assert by_day[bullish_day - timedelta(days=1)].state == SmaState.MIXED


def test_staying_above_does_not_repeat():
    points = compute_states(series(*range(1, 60)))
    assert compute_events(points) == []  # ABOVE from the first point, never flips


def test_direct_flip_above_to_below_fires():
    from tradio.signals import StatePoint

    def p(day, state):
        return StatePoint(START + timedelta(days=day), Decimal(1), Decimal(1), Decimal(1), Decimal(1), state)

    events = compute_events([p(0, SmaState.ABOVE), p(1, SmaState.BELOW)])
    assert [e.direction for e in events] == [Direction.BEARISH]
