"""Replay the signal over stored history, without writing anything."""

from statistics import median

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tradio.adjustments import adjusted_closes
from tradio.models import DailyPrice
from tradio.signals import compute_events, compute_states

FORWARD_DAYS = 10
TRADING_DAYS_PER_YEAR = 248


def detail(session: Session, symbols: list[str], last: int) -> str:
    lines = []
    for symbol, closes in sorted(adjusted_closes(session, symbols).items()):
        points = compute_states(closes)
        events = compute_events(points)
        index = {p.trade_date: i for i, p in enumerate(points)}
        lines.append(
            f"\n{symbol}: {len(events)} events over {len(points)} trading days "
            f"({closes[0][0]} to {closes[-1][0]})"
        )
        lines.append(f"  {'date':<10}  {'dir':<7}  {'close':>10}  {'sma8':>10}  {'sma13':>10}  {'sma21':>10}  {f'{FORWARD_DAYS}d move':>9}")
        for e in events[-last:]:
            p, i = e.point, index[e.point.trade_date]
            move = (
                f"{(points[i + FORWARD_DAYS].close / p.close - 1) * 100:+.1f}%"
                if i + FORWARD_DAYS < len(points)
                else "-"
            )
            lines.append(
                f"  {p.trade_date}  {e.direction.value:<7}  {p.close:>10.2f}  {p.sma8:>10.2f}  "
                f"{p.sma13:>10.2f}  {p.sma21:>10.2f}  {move:>9}"
            )
    return "\n".join(lines) or "no data for those symbols"


def summary(session: Session, min_days: int = TRADING_DAYS_PER_YEAR) -> str:
    """How often the signal fires per stock per year, across every stock with enough history."""
    symbols = list(
        session.scalars(
            select(DailyPrice.symbol).group_by(DailyPrice.symbol).having(func.count() >= min_days)
        )
    )
    rates = {}
    for symbol, closes in adjusted_closes(session, symbols).items():
        points = compute_states(closes)
        rates[symbol] = len(compute_events(points)) / len(points) * TRADING_DAYS_PER_YEAR

    ordered = sorted(rates.values())
    pct = lambda q: ordered[int(q * (len(ordered) - 1))]  # noqa: E731
    return (
        f"{len(rates)} stocks with >= {min_days} trading days\n"
        f"events per stock per year: median {median(ordered):.1f}, "
        f"p10 {pct(0.1):.1f}, p90 {pct(0.9):.1f}, max {ordered[-1]:.1f}"
    )
