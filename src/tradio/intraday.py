"""Confirmed 15-minute SMA crossovers. See docs/v3-spec.md.

The old per-day provisional records remain only for evening reconciliation of
alerts created before v3. New intraday alerts are keyed by candle timestamp.
"""

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from telegram import Bot
from telegram.constants import ParseMode

from tradio import messages
from tradio.alerts import add_alert, notify_once, queue_deliveries, send_pending, watched_symbols
from tradio.config import IST
from tradio.daily import load_and_alert
from tradio.data.base import MarketDataSource
from tradio.data.upstox import Candle, LivePriceSource, MarketHours
from tradio.db import in_session
from tradio.models import AlertKind, CandlePrice, CandleProgress, DailyPrice, Direction, Instrument, PriceAdjustment, SmaState
from tradio.signals import classify
from tradio.watchlist import latest_trading_day

log = logging.getLogger(__name__)
INTERVAL = timedelta(minutes=15)
PUBLISH_DELAY = timedelta(seconds=5)
LOOKBACK = timedelta(days=28)  # within Upstox's one-month request limit
EXPIRY_REMINDERS = (30, 7, 1)
MAX_GAP_DAYS = 30
ACTION_TOLERANCE = Decimal("0.002")


class StaleHistory(Exception):
    """Official daily data is too old to check today's corporate-action adjustment."""


@dataclass(frozen=True)
class CandlePoint:
    start: datetime
    close: Decimal
    sma8: Decimal
    sma13: Decimal
    sma21: Decimal
    state: SmaState
    direction: Direction | None


def next_check(now: datetime, hours: MarketHours | None = None) -> datetime:
    """Next candle close plus delay; outside a session, keep daily quarter-hour ticks."""
    if hours is not None and hours.start <= now <= hours.end + INTERVAL + PUBLISH_DELAY:
        number = max(1, int((now - hours.start - PUBLISH_DELAY) // INTERVAL) + 1)
        candidate = hours.start + number * INTERVAL + PUBLISH_DELAY
        if candidate <= hours.end + INTERVAL + PUBLISH_DELAY:
            return candidate
    quarter = now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0) + PUBLISH_DELAY
    return quarter if quarter > now else quarter + INTERVAL


def in_hours(hours: MarketHours, now: datetime) -> bool:
    # Allow scheduler jitter on the final retry without admitting another candle.
    return hours.start + INTERVAL + PUBLISH_DELAY <= now < hours.end + INTERVAL + PUBLISH_DELAY + timedelta(minutes=1)


def history_is_current(live: LivePriceSource, latest: date | None, today: date) -> bool:
    if latest is None or (today - latest).days > MAX_GAP_DAYS:
        return False
    return all(live.market_hours(latest + timedelta(days=i)) is None for i in range(1, (today - latest).days))


def expected_starts(live: LivePriceSource, first: date, now: datetime) -> list[datetime]:
    """Session-aligned, fully closed candles only; special sessions use actual hours."""
    starts = []
    for i in range((now.date() - first).days + 1):
        hours = live.market_hours(first + timedelta(days=i))
        if hours is None:
            continue
        stamp = hours.start
        while stamp + INTERVAL <= hours.end and stamp + INTERVAL + PUBLISH_DELAY <= now:
            starts.append(stamp)
            stamp += INTERVAL
    return starts


def evaluate_candles(candles: list[Candle], calendar: list[datetime]) -> list[CandlePoint]:
    """Classify each complete candle, resetting across missing trading candles.

    Directions alternate. The first state after warm-up is a baseline, never an
    alert. No requirement that SMA13 exceed SMA21 or price exceed the averages.
    """
    positions = {stamp: i for i, stamp in enumerate(calendar)}
    window = deque(maxlen=21)
    previous_position = None
    previous_state = None
    last_direction = None
    points = []
    for candle in sorted(candles, key=lambda c: c.start):
        position = positions.get(candle.start)
        if position is None:
            continue
        if previous_position is not None and position != previous_position + 1:
            window.clear()
            previous_state = last_direction = None
        previous_position = position
        window.append(candle.close)
        if len(window) < 21:
            continue
        values = list(window)
        sma8, sma13, sma21 = (sum(values[-n:], Decimal(0)) / n for n in (8, 13, 21))
        state = classify(sma8, sma13, sma21)
        direction = None
        if previous_state is not None and state != previous_state and state != SmaState.MIXED:
            candidate = Direction.BULLISH if state == SmaState.ABOVE else Direction.BEARISH
            if candidate != last_direction:
                direction = last_direction = candidate
        points.append(CandlePoint(candle.start, candle.close, sma8, sma13, sma21, state, direction))
        previous_state = state
    return points


def action_reset(session: Session, symbol: str, live: LivePriceSource, isin: str, today: date) -> date | None:
    """Conservatively warm up again after a corporate action, avoiding fake crosses.

    Candle history is raw. Known NSE actions reset the window on the ex-date;
    Upstox's adjusted previous close detects an action before NSE's evening file.
    Compare with the official daily close, never the last intraday candle.
    """
    latest_action = session.scalar(select(PriceAdjustment.ex_date).where(
        PriceAdjustment.symbol == symbol, PriceAdjustment.ex_date <= today
    ).order_by(PriceAdjustment.ex_date.desc()).limit(1))
    previous = session.scalar(select(DailyPrice).where(
        DailyPrice.symbol == symbol, DailyPrice.trade_date < today
    ).order_by(DailyPrice.trade_date.desc()).limit(1))
    if previous is not None and previous.close > 0:
        quote = live.quotes([isin]).get(isin)
        if quote is None:
            raise ValueError(f"missing previous-close quote for {symbol}")
        if abs(quote.prev_close / previous.close - 1) > ACTION_TOLERANCE:
            latest_action = today
    return latest_action


def evaluate_live(session: Session, live: LivePriceSource, now: datetime, *, store: bool = True) -> list[tuple[str, list[CandlePoint]]]:
    """Fetch/cache candle closes; dry runs use the same analysis without writes."""
    today = now.astimezone(IST).date()
    first = today - LOOKBACK
    calendar = expected_starts(live, first, now)
    if not calendar:
        return []
    instruments = session.scalars(select(Instrument).where(Instrument.symbol.in_(watched_symbols(session)))).all()
    results = []
    for instrument in instruments:
        symbol, isin = instrument.symbol, instrument.isin
        progress = session.get(CandleProgress, symbol)
        candles = {row.start: Candle(row.start, row.close) for row in session.scalars(select(CandlePrice).where(
            CandlePrice.symbol == symbol, CandlePrice.start >= datetime.combine(first, datetime.min.time(), IST)
        ))}
        # Fetch historical data once per session (also catches up missed days).
        fetched = []
        if progress is None or progress.history_through != today - timedelta(days=1):
            fetched.extend(live.candles(isin, today - timedelta(days=1), first))
        fetched.extend(live.candles(isin, today))
        allowed = set(calendar)
        fetched = [c for c in fetched if c.start in allowed]
        candles.update((c.start, c) for c in fetched)
        reset_day = action_reset(session, symbol, live, isin, today)
        usable = [c for c in candles.values() if reset_day is None or c.start.date() >= reset_day]
        points = evaluate_candles(usable, calendar)
        results.append((symbol, points))
        if store:
            if fetched:
                values = [{"symbol": symbol, "start": c.start, "close": c.close} for c in {c.start: c for c in fetched}.values()]
                stmt = insert(CandlePrice).values(values)
                session.execute(stmt.on_conflict_do_update(index_elements=[CandlePrice.symbol, CandlePrice.start],
                                                          set_={"close": stmt.excluded.close}))
            if progress is None:
                progress = CandleProgress(symbol=symbol)
                session.add(progress)
            progress.history_through = today - timedelta(days=1)
    return results


def record(session: Session, results: list[tuple[str, list[CandlePoint]]], now: datetime) -> list[int]:
    ids = []
    for symbol, points in results:
        if not points:
            continue
        progress = session.get(CandleProgress, symbol)
        cutoff = progress.last_candle
        # New stocks seed from history, alert only on the most recent completed
        # candle. Existing stocks replay every missed candle in timestamp order.
        pending = [p for p in points if cutoff is not None and p.start > cutoff]
        if cutoff is None:
            pending = [p for p in points[-1:] if now - (p.start + INTERVAL + PUBLISH_DELAY) < INTERVAL]
        last_direction = progress.last_direction
        for p in pending:
            if p.direction is None or p.direction == last_direction:
                continue
            last_direction = p.direction
            # Old events advance state without delivering stale trading alerts.
            if p.start.date() < now.date() - timedelta(days=5):
                continue
            alert_id = add_alert(session, symbol=symbol, trade_date=p.start.date(), kind=AlertKind.CANDLE,
                                 seq=p.start.hour * 60 + p.start.minute, direction=p.direction,
                                 price=p.close, sma8=p.sma8, sma13=p.sma13, sma21=p.sma21,
                                 candle_start=p.start, created_at=now)
            if alert_id is not None:
                ids.append(alert_id)
        # Seed alternation even when the last historical event wasn't delivered.
        if cutoff is None:
            last_direction = next((p.direction for p in reversed(points) if p.direction is not None), None)
        progress.last_direction = last_direction
        progress.last_candle = points[-1].start
    queue_deliveries(session, ids)
    return ids


def run_once(session: Session, live: LivePriceSource, nse: MarketDataSource, today: date, now: datetime) -> list[int]:
    # Serialise ticks across pods/CLI invocations. Alert insertion and progress
    # commit atomically; a pod dying mid-tick rolls both back.
    if not session.scalar(text("SELECT pg_try_advisory_xact_lock(73152115)")):
        return []
    if not history_is_current(live, latest_trading_day(session), today):
        load_and_alert(session, nse, today - timedelta(days=1))
        # load_and_alert commits; reacquire the transaction-scoped lock.
        if not session.scalar(text("SELECT pg_try_advisory_xact_lock(73152115)")):
            return []
        if not history_is_current(live, latest_trading_day(session), today):
            raise StaleHistory("NSE daily history is behind; corporate-action checks are unsafe")
    ids = record(session, evaluate_live(session, live, now), now)
    session.execute(delete(CandlePrice).where(CandlePrice.start < datetime.combine(today - LOOKBACK, datetime.min.time(), IST)))
    session.commit()
    return ids


async def tick(bot: Bot, live: LivePriceSource, nse: MarketDataSource, now: datetime, admin_id: int | None) -> list[int] | None:
    now = now.astimezone(IST)
    try:
        hours = await asyncio.to_thread(live.market_hours, now.date())
        if hours is None or not in_hours(hours, now):
            return None
        ids = await in_session(run_once, live, nse, now.date(), now)
    except Exception as exc:
        log.exception("15-minute candle check failed")
        if admin_id and await in_session(notify_once, f"intraday-failed:{now.date()}"):
            await bot.send_message(admin_id, messages.intraday_failed(now.date(), exc), parse_mode=ParseMode.HTML)
        return None
    sent, failed = await send_pending(bot)
    log.info("15m check %s: %d new candle alerts, sent to %d users, %d failed",
             now.strftime("%Y-%m-%d %H:%M:%S"), len(ids), sent, failed)
    return ids


async def remind_expiry(bot: Bot, expiry: datetime | None, now: datetime, admin_id: int | None) -> bool:
    if expiry is None or admin_id is None:
        return False
    days_left = (expiry - now).days
    due = [f"upstox-expiry:{expiry:%Y-%m-%d}:{n}" for n in EXPIRY_REMINDERS if days_left < n]
    if not due or not await in_session(notify_once, *due):
        return False
    await bot.send_message(admin_id, messages.token_expiring(expiry, days_left), parse_mode=ParseMode.HTML)
    return True
