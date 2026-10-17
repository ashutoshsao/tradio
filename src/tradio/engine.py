"""Compute and store SMA states and events for a set of stocks."""

from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from tradio.adjustments import adjusted_closes
from tradio.models import SignalEvent, SignalState, TradingDay
from tradio.signals import analyse


def trading_calendar(session: Session) -> list[date]:
    return list(session.scalars(select(TradingDay.trade_date).order_by(TradingDay.trade_date)))


def evaluate(session: Session, symbols: list[str]) -> list[int]:
    """Recompute full state history for `symbols` and record any events.

    States are rewritten every time because a newly detected split changes
    history. Events are only ever added. Returns ids of newly added events.
    """
    calendar = trading_calendar(session)
    new_event_ids: list[int] = []
    for symbol, closes in adjusted_closes(session, symbols).items():
        points, events = analyse(closes, calendar)
        if not points:
            continue

        states = insert(SignalState).values(
            [
                {"symbol": symbol, "trade_date": p.trade_date, "sma8": p.sma8, "sma13": p.sma13, "sma21": p.sma21, "state": p.state}
                for p in points
            ]
        )
        session.execute(
            states.on_conflict_do_update(
                index_elements=[SignalState.symbol, SignalState.trade_date],
                set_={c: states.excluded[c] for c in ("sma8", "sma13", "sma21", "state")},
            )
        )

        if events:
            stmt = (
                insert(SignalEvent)
                .values(
                    [
                        {
                            "symbol": symbol,
                            "trade_date": e.point.trade_date,
                            "direction": e.direction,
                            "close": e.point.close,
                            "sma8": e.point.sma8,
                            "sma13": e.point.sma13,
                            "sma21": e.point.sma21,
                        }
                        for e in events
                    ]
                )
                .on_conflict_do_nothing()
                .returning(SignalEvent.id)
            )
            new_event_ids.extend(session.scalars(stmt))
    return new_event_ids
