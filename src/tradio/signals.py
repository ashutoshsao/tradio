"""SMA 8/13/21 state-flip signal. See docs/v1-spec.md §3. Pure functions, no I/O."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from tradio.models import Direction, SmaState

FAST, MID, SLOW = 8, 13, 21


@dataclass(frozen=True)
class StatePoint:
    trade_date: date
    close: Decimal
    sma8: Decimal
    sma13: Decimal
    sma21: Decimal
    state: SmaState


@dataclass(frozen=True)
class Event:
    point: StatePoint
    direction: Direction


def classify(sma8: Decimal, sma13: Decimal, sma21: Decimal) -> SmaState:
    if sma8 > sma13 and sma8 > sma21:
        return SmaState.ABOVE
    if sma8 < sma13 and sma8 < sma21:
        return SmaState.BELOW
    return SmaState.MIXED


def compute_states(closes: Sequence[tuple[date, Decimal]]) -> list[StatePoint]:
    """One point per day from the 21st close onwards. `closes` must be in date order."""
    values = [c for _, c in closes]
    # Running prefix sums keep this O(n); Decimal addition is exact so nothing drifts.
    prefix = [Decimal(0)]
    for v in values:
        prefix.append(prefix[-1] + v)

    points = []
    for i in range(SLOW - 1, len(values)):
        sma8, sma13, sma21 = ((prefix[i + 1] - prefix[i + 1 - n]) / n for n in (FAST, MID, SLOW))
        points.append(StatePoint(closes[i][0], values[i], sma8, sma13, sma21, classify(sma8, sma13, sma21)))
    return points


def compute_events(points: Sequence[StatePoint]) -> list[Event]:
    """A flip into ABOVE or BELOW from any other state. The first point never fires (no prior state)."""
    events = []
    for prev, cur in zip(points, points[1:]):
        if cur.state != prev.state and cur.state != SmaState.MIXED:
            events.append(Event(cur, Direction.BULLISH if cur.state == SmaState.ABOVE else Direction.BEARISH))
    return events
