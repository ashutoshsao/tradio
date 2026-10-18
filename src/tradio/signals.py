"""SMA 8/13/21 state-flip signal. See docs/v1-spec.md §3. Pure functions, no I/O."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from tradio.models import Direction, SmaState

FAST, MID, SLOW = 8, 13, 21
# A stock missing for longer than this (suspension, moved to the BE series, a
# demerger's trade-for-trade period) restarts its history: averaging across the
# hole would compare prices from before and after an unknown change.
MAX_MISSING_DAYS = 5
# Intraday cross alerts per stock per day (docs/v2-spec.md §4).
MAX_CROSSES = 3


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
    """A flip into ABOVE or BELOW from any other state, in the opposite direction to the last event.

    BELOW -> MIXED -> BELOW is SMA8 bouncing off one average, not a new cross,
    so it doesn't repeat the bearish alert. The first point never fires (no prior state).
    """
    events: list[Event] = []
    for prev, cur in zip(points, points[1:]):
        if cur.state == prev.state or cur.state == SmaState.MIXED:
            continue
        direction = Direction.BULLISH if cur.state == SmaState.ABOVE else Direction.BEARISH
        if not events or events[-1].direction != direction:
            events.append(Event(cur, direction))
    return events


def split_on_gaps(
    closes: Sequence[tuple[date, Decimal]], calendar: Sequence[date], max_missing: int = MAX_MISSING_DAYS
) -> list[list[tuple[date, Decimal]]]:
    """Split a stock's closes into runs with no more than `max_missing` absent market days.

    `calendar` is every market trading day, in order.
    """
    position = {d: i for i, d in enumerate(calendar)}
    segments: list[list[tuple[date, Decimal]]] = []
    prev = None
    for day, close in closes:
        i = position[day]
        if prev is None or i - prev - 1 > max_missing:
            segments.append([])
        segments[-1].append((day, close))
        prev = i
    return segments


def analyse(closes: Sequence[tuple[date, Decimal]], calendar: Sequence[date]) -> tuple[list[StatePoint], list[Event]]:
    """States and events over a stock's whole history, restarting after each data gap."""
    points, events = [], []
    for segment in split_on_gaps(closes, calendar):
        seg_points = compute_states(segment)
        points.extend(seg_points)
        events.extend(compute_events(seg_points))
    return points, events
