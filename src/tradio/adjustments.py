"""Split/bonus adjustment. See docs/v1-spec.md §4.

On an ex-date NSE reports the previous close already adjusted for the corporate
action, so prev_close(t) / close(t-1) is the adjustment factor. Raw prices stay
untouched; factors are stored and applied on read.
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from tradio.models import DailyPrice, PriceAdjustment

# Ordinary days differ only by rounding, so anything beyond this is a corporate action.
THRESHOLD = Decimal("0.01")

_DETECT_SQL = text(
    """
    WITH days AS (
        SELECT trade_date, LAG(trade_date) OVER (ORDER BY trade_date) AS market_prev_day
        FROM (SELECT DISTINCT trade_date FROM daily_prices) d
    ),
    p AS (
        SELECT symbol, trade_date, prev_close,
               LAG(close) OVER w AS last_close,
               LAG(trade_date) OVER w AS last_day
        FROM daily_prices
        WINDOW w AS (PARTITION BY symbol ORDER BY trade_date)
    )
    INSERT INTO price_adjustments (symbol, ex_date, factor)
    SELECT p.symbol, p.trade_date, p.prev_close / p.last_close
    FROM p JOIN days USING (trade_date)
    -- Only compare consecutive market days: after a gap (suspension, BE series)
    -- the reported prev_close refers to a close we don't have.
    WHERE p.last_day = days.market_prev_day
      AND p.last_close > 0
      AND abs(p.prev_close / p.last_close - 1) > :threshold
    ON CONFLICT DO NOTHING
    """
)


def detect_adjustments(session: Session) -> int:
    """Record any new split/bonus factors. Returns how many were added."""
    return session.execute(_DETECT_SQL, {"threshold": THRESHOLD}).rowcount


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
