"""Split/bonus/demerger adjustment. See docs/v1-spec.md §4.

Factors come from NSE's corporate actions data. (The bhavcopy's previous close
is NOT adjusted on ex-dates, so prices alone can't tell a split from a crash.)
Raw prices stay untouched; factors are stored and applied on read.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from tradio.data.base import CorporateAction
from tradio.models import DailyPrice, PriceAdjustment

log = logging.getLogger(__name__)

_EX_DATE_GAP_SQL = text(
    """
    SELECT p.open / prev.close
    FROM daily_prices p
    JOIN LATERAL (
        SELECT close FROM daily_prices
        WHERE symbol = p.symbol AND trade_date < p.trade_date
        ORDER BY trade_date DESC LIMIT 1
    ) prev ON prev.close > 0
    WHERE p.symbol = :symbol AND p.trade_date = :ex_date
    """
)


def ex_date_gap(session: Session, symbol: str, ex_date: date) -> Decimal | None:
    """Open on the ex-date over the previous close, or None if either price is missing."""
    return session.scalar(_EX_DATE_GAP_SQL, {"symbol": symbol, "ex_date": ex_date})


def store_actions(session: Session, actions: list[CorporateAction]) -> int:
    """Upsert actions. Same (symbol, ex_date, action) from another source is stored once.

    Demergers carry no ratio, so their factor is the ex-date price gap; they're
    skipped (and picked up on a later run) until those prices are stored.
    Returns how many were stored.
    """
    rows = {}
    for a in actions:
        factor = a.factor if a.factor is not None else ex_date_gap(session, a.symbol, a.ex_date)
        if factor is None:
            log.debug("%s %s %s: no prices around the ex-date yet, skipped", a.ex_date, a.symbol, a.action)
            continue
        rows[(a.symbol, a.ex_date, a.action)] = {
            "symbol": a.symbol,
            "ex_date": a.ex_date,
            "action": a.action,
            "factor": factor,
            "purpose": a.purpose,
            "source": a.source,
        }
    if not rows:
        return 0
    stmt = insert(PriceAdjustment).values(list(rows.values()))
    # First source to report an action wins; the factor is the same either way.
    session.execute(stmt.on_conflict_do_nothing())
    return len(rows)


@dataclass(frozen=True)
class GapCheck:
    symbol: str
    ex_date: date
    actions: str
    factor: Decimal
    gap: Decimal | None  # open on ex-date / previous close; None if no prices around the ex-date


_GAP_SQL = text(
    """
    SELECT a.symbol, a.ex_date, string_agg(a.action, ' + ' ORDER BY a.action),
           exp(sum(ln(a.factor))), p.open / prev.close
    FROM price_adjustments a
    LEFT JOIN daily_prices p ON p.symbol = a.symbol AND p.trade_date = a.ex_date
    LEFT JOIN LATERAL (
        SELECT close FROM daily_prices
        WHERE symbol = a.symbol AND trade_date < a.ex_date
        ORDER BY trade_date DESC LIMIT 1
    ) prev ON true
    -- Demerger factors are the gap by construction, so there's nothing to check.
    WHERE a.action <> 'demerger'
    GROUP BY a.symbol, a.ex_date, p.open, prev.close
    ORDER BY a.ex_date
    """
)


def gap_checks(session: Session) -> list[GapCheck]:
    """Each split/bonus factor next to the actual overnight price gap, for sanity checking."""
    return [GapCheck(*row) for row in session.execute(_GAP_SQL)]


def adjusted_closes(session: Session, symbols: list[str], since: date | None = None) -> dict[str, list[tuple[date, Decimal]]]:
    """Closes per symbol in date order, scaled so they're comparable across splits/bonuses."""
    prices = select(DailyPrice.symbol, DailyPrice.trade_date, DailyPrice.close).where(DailyPrice.symbol.in_(symbols))
    if since:
        prices = prices.where(DailyPrice.trade_date >= since)

    factors: dict[str, list[tuple[date, Decimal]]] = defaultdict(list)
    for symbol, ex_date, factor in session.execute(
        select(PriceAdjustment.symbol, PriceAdjustment.ex_date, PriceAdjustment.factor).where(
            PriceAdjustment.symbol.in_(symbols)
        )
    ):
        factors[symbol].append((ex_date, factor))

    raw: dict[str, list[tuple[date, Decimal]]] = defaultdict(list)
    for symbol, day, close in session.execute(prices.order_by(DailyPrice.symbol, DailyPrice.trade_date)):
        raw[symbol].append((day, close))

    result = {}
    for symbol, closes in raw.items():
        pending = sorted(factors[symbol], reverse=True)
        multiplier = Decimal(1)
        adjusted = []
        # Walk newest to oldest, picking up each factor as we pass its ex-date.
        for day, close in reversed(closes):
            while pending and pending[0][0] > day:
                multiplier *= pending.pop(0)[1]
            adjusted.append((day, close * multiplier))
        result[symbol] = adjusted[::-1]
    return result
