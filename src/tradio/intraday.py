"""Intraday run: the daily signal on today's live price, alerted as provisional crosses.

See docs/v2-spec.md §3–4.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session
from telegram import Bot
from telegram.constants import ParseMode

from tradio import messages
from tradio.adjustments import adjusted_closes
from tradio.alerts import add_alert, notify_once, queue_deliveries, send_pending, watched_symbols
from tradio.config import IST
from tradio.daily import load_and_alert
from tradio.data.base import MarketDataSource
from tradio.data.upstox import LivePriceSource, MarketHours, Quote
from tradio.db import in_session
from tradio.engine import trading_calendar
from tradio.models import AlertKind, Direction, Instrument, IntradaySignal
from tradio.signals import MAX_CROSSES, StatePoint, analyse
from tradio.watchlist import latest_trading_day

log = logging.getLogger(__name__)

EDGE = timedelta(minutes=15)  # no checks this soon after the open or before the close
ACTION_TOLERANCE = Decimal("0.002")  # prev close vs our close beyond this = corporate action today
MAX_GAP_DAYS = 30  # calendar days checked for missed sessions before giving up
EXPIRY_REMINDERS = (30, 7, 1)  # days before the Upstox token expires


class StaleHistory(Exception):
    """Our closes stop before the previous NSE session, so today's SMAs would be off by a day."""


@dataclass(frozen=True)
class Provisional:
    symbol: str
    point: StatePoint  # today, with the live price as the close
    direction: Direction | None  # a cross today, if any
    factor: Decimal  # today's corporate-action factor applied to history (1 if none)


def in_hours(hours: MarketHours, now: datetime) -> bool:
    return hours.start + EDGE <= now <= hours.end - EDGE


def history_is_current(live: LivePriceSource, latest: date | None, today: date) -> bool:
    """True if no NSE session happened between our latest stored day and today."""
    if latest is None or (today - latest).days > MAX_GAP_DAYS:
        return False
    day = latest + timedelta(days=1)
    while day < today:
        if live.market_hours(day) is not None:
            return False
        day += timedelta(days=1)
    return True


def provisional(
    symbol: str, closes: list[tuple[date, Decimal]], quote: Quote, calendar: list[date], today: date
) -> Provisional | None:
    """Today's state with the live price as the close. None if the stock lacks history."""
    if not closes:
        return None
    factor = Decimal(1)
    last_day, last_close = closes[-1]
    if last_day == calendar[-1]:
        # Upstox's previous close is already adjusted on an ex-date; ours isn't until the evening files.
        ratio = quote.prev_close / last_close
        if abs(ratio - 1) > ACTION_TOLERANCE:
            factor = ratio
            closes = [(d, c * factor) for d, c in closes]
    points, events = analyse([*closes, (today, quote.price)], [*calendar, today])
    if not points or points[-1].trade_date != today:
        return None
    direction = events[-1].direction if events and events[-1].point.trade_date == today else None
    return Provisional(symbol, points[-1], direction, factor)


def evaluate_live(session: Session, live: LivePriceSource, today: date) -> list[Provisional]:
    symbols = watched_symbols(session)
    calendar = trading_calendar(session)
    if not symbols or not calendar or calendar[-1] >= today:
        return []  # nothing to watch, or today's official close is already loaded
    isins = dict(session.execute(select(Instrument.symbol, Instrument.isin).where(Instrument.symbol.in_(symbols))).all())
    quotes = live.quotes(sorted(set(isins.values())))
    closes = adjusted_closes(session, symbols)
    results = []
    for symbol in sorted(symbols):
        quote = quotes.get(isins[symbol])
        if quote is None:
            log.warning("no live quote for %s (%s)", symbol, isins[symbol])
            continue
        if (p := provisional(symbol, closes.get(symbol, []), quote, calendar, today)) is not None:
            if p.factor != 1:
                log.info("%s: corporate action today, history scaled by %s", symbol, p.factor)
            results.append(p)
    return results


def record(session: Session, results: list[Provisional], today: date, now: datetime) -> list[int]:
    """Update each stock's state for the day; returns ids of new cross/undone alerts."""
    alert_ids = []
    for p in results:
        row = session.get(IntradaySignal, (p.symbol, today))
        crossed = p.direction is not None
        if row is None:
            if not crossed:
                continue
            row = IntradaySignal(symbol=p.symbol, trade_date=today, direction=p.direction, active=False, crosses=0)
            session.add(row)

        kind = None
        if crossed and not row.active:
            row.active, row.direction = True, p.direction
            row.crosses += 1
            if row.crosses <= MAX_CROSSES:
                kind = AlertKind.CROSS
        elif not crossed and row.active:
            row.active = False
            if row.crosses < MAX_CROSSES:  # the third cross alert already said "no more today"
                kind = AlertKind.UNDONE
        row.price, row.sma8, row.sma13, row.sma21 = p.point.close, p.point.sma8, p.point.sma13, p.point.sma21
        row.updated_at = now

        if kind:
            alert_ids.append(
                add_alert(
                    session, symbol=p.symbol, trade_date=today, kind=kind, seq=row.crosses, direction=row.direction,
                    price=p.point.close, sma8=p.point.sma8, sma13=p.point.sma13, sma21=p.point.sma21, created_at=now,
                )
            )
    return [i for i in alert_ids if i is not None]


def run_once(session: Session, live: LivePriceSource, nse: MarketDataSource, today: date, now: datetime) -> list[int]:
    """One intraday evaluation. Returns ids of new cross/undone alerts."""
    latest = latest_trading_day(session)
    if latest is not None and latest >= today:
        return []  # today's official close is already loaded
    if not history_is_current(live, latest, today):
        # The evening run was missed (e.g. laptop asleep): do it now; its close alerts are queued too.
        log.info("history ends %s; catching up before the intraday check", latest)
        load_and_alert(session, nse, today - timedelta(days=1))
        if not history_is_current(live, latest_trading_day(session), today):
            raise StaleHistory(f"latest NSE data is {latest_trading_day(session)}")
    alert_ids = record(session, evaluate_live(session, live, today), today, now)
    queue_deliveries(session, alert_ids)
    session.commit()
    return alert_ids


async def tick(bot: Bot, live: LivePriceSource, nse: MarketDataSource, now: datetime, admin_id: int | None) -> list[int] | None:
    """Run if the market is open (past the first 15 minutes). Returns new alert ids, or None if skipped."""
    today = now.astimezone(IST).date()
    try:
        hours = await asyncio.to_thread(live.market_hours, today)
        if hours is None or not in_hours(hours, now):
            return None
        alert_ids = await in_session(run_once, live, nse, today, now)
    except Exception as exc:
        log.exception("intraday tick failed")
        if admin_id and await in_session(notify_once, f"intraday-failed:{today}"):
            await bot.send_message(admin_id, messages.intraday_failed(today, exc), parse_mode=ParseMode.HTML)
        return None
    sent, failed = await send_pending(bot)
    log.info("intraday %s: %d alerts, sent to %d users, %d failed", now.astimezone(IST).strftime("%H:%M"), len(alert_ids), sent, failed)
    return alert_ids


async def remind_expiry(bot: Bot, expiry: datetime | None, now: datetime, admin_id: int | None) -> bool:
    """Tell the admin 30, 7 and 1 days before the Upstox token expires (once each)."""
    if expiry is None or admin_id is None:
        return False
    days_left = (expiry - now).days
    due = [f"upstox-expiry:{expiry:%Y-%m-%d}:{n}" for n in EXPIRY_REMINDERS if days_left < n]
    if not due or not await in_session(notify_once, *due):
        return False
    await bot.send_message(admin_id, messages.token_expiring(expiry, days_left), parse_mode=ParseMode.HTML)
    return True
