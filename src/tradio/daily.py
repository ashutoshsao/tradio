"""The evening run: load new NSE data, find crosses, send digests. See docs/v1-spec.md §6.

Every tick is idempotent, so it can run every 15 minutes from the bot's job
queue or as a Kubernetes CronJob.
"""

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError

from tradio import messages
from tradio.data.base import MarketDataSource
from tradio.db import session as new_session
from tradio.engine import evaluate, trading_calendar
from tradio.ingest import ingest_day, sync_actions_api
from tradio.models import Delivery, DeliveryStatus, Instrument, Run, RunStatus, SignalEvent, TradingDay, User, WatchlistEntry
from tradio.watchlist import AlertItem

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
WINDOW_START, WINDOW_END = time(18, 0), time(22, 0)
MAX_ALERT_AGE = 5  # trading days; older crosses (e.g. after a long laptop sleep) aren't sent
MAX_ATTEMPTS = 5
ACTIONS_API_LOOKBACK = timedelta(days=14)
BACKFILL_START_IF_EMPTY = timedelta(days=60)


# --- Loading and evaluating (sync, runs in a worker thread) ---------------------


def ingest_new_days(session: Session, source: MarketDataSource, today: date) -> list[date]:
    """Load trading days after the last one stored, and retry recent days missing corporate actions."""
    last = session.scalar(select(func.max(TradingDay.trade_date)))
    start = last + timedelta(days=1) if last else today - BACKFILL_START_IF_EMPTY
    retry = session.scalars(
        select(TradingDay.trade_date).where(
            TradingDay.trade_date >= today - ACTIONS_API_LOOKBACK, TradingDay.actions_loaded.is_(False)
        )
    )
    new_days = []
    for day in sorted({*retry, *(start + timedelta(days=i) for i in range((today - start).days + 1))}):
        is_new = session.get(TradingDay, day) is None
        if ingest_day(session, source, day) and is_new:
            new_days.append(day)
    sync_actions_api(session, source, today - ACTIONS_API_LOOKBACK, today)
    return new_days


def watched_symbols(session: Session) -> list[str]:
    return list(
        session.scalars(
            select(WatchlistEntry.symbol)
            .join(User, User.telegram_user_id == WatchlistEntry.user_id)
            .where(User.is_allowed)
            .distinct()
        )
    )


def queue_deliveries(session: Session, event_ids: list[int]) -> int:
    """One pending delivery per (new event, allowed user watching it), for recent events only."""
    if not event_ids:
        return 0
    calendar = trading_calendar(session)
    oldest = calendar[-MAX_ALERT_AGE] if len(calendar) >= MAX_ALERT_AGE else date.min
    pairs = session.execute(
        select(SignalEvent.id, WatchlistEntry.user_id)
        .join(WatchlistEntry, WatchlistEntry.symbol == SignalEvent.symbol)
        .join(User, User.telegram_user_id == WatchlistEntry.user_id)
        .where(SignalEvent.id.in_(event_ids), SignalEvent.trade_date >= oldest, User.is_allowed)
    ).all()
    if pairs:
        session.execute(
            insert(Delivery).values([{"event_id": e, "user_id": u} for e, u in pairs]).on_conflict_do_nothing()
        )
    return len(pairs)


@dataclass
class Outbox:
    chat_id: int
    deliveries: list[tuple[int, int]] = field(default_factory=list)  # (event_id, user_id)
    alerts: list[AlertItem] = field(default_factory=list)


def pending_outboxes(session: Session) -> dict[int, Outbox]:
    rows = session.execute(
        select(Delivery, SignalEvent, Instrument.name, User.chat_id)
        .join(SignalEvent, SignalEvent.id == Delivery.event_id)
        .join(Instrument, Instrument.symbol == SignalEvent.symbol)
        .join(User, User.telegram_user_id == Delivery.user_id)
        .where(
            Delivery.status.in_([DeliveryStatus.PENDING, DeliveryStatus.FAILED]),
            Delivery.attempts < MAX_ATTEMPTS,
            User.is_allowed,
        )
    )
    boxes: dict[int, Outbox] = {}
    for d, e, name, chat_id in rows:
        box = boxes.setdefault(d.user_id, Outbox(chat_id))
        box.deliveries.append((d.event_id, d.user_id))
        box.alerts.append(AlertItem(e.symbol, name, e.trade_date, e.direction, e.close, e.sma8, e.sma13, e.sma21))
    return boxes


def mark(session: Session, keys: list[tuple[int, int]], status: DeliveryStatus, error: str | None = None) -> None:
    for event_id, user_id in keys:
        d = session.get(Delivery, (event_id, user_id))
        d.status = status
        d.attempts += 1
        d.error = error
        if status == DeliveryStatus.SENT:
            d.sent_at = datetime.now(IST)
    session.commit()


def start_run(session: Session, day: date) -> Run:
    run = session.get(Run, day) or Run(trade_date=day, status=RunStatus.RUNNING, attempts=0)
    if run.status != RunStatus.DONE:
        run.status = RunStatus.RUNNING  # keep `error`, so a repeat failure isn't re-announced
    run.attempts += 1
    session.add(run)
    session.commit()
    return run


def finish_run(session: Session, day: date, status: RunStatus, error: str | None = None) -> bool:
    """Returns True if this is the day's first failure (so the admin is told once, not every tick)."""
    run = session.get(Run, day)
    first_failure = status == RunStatus.FAILED and run.error is None
    run.status, run.error, run.finished_at = status, error, datetime.now(IST)
    session.commit()
    return first_failure


# --- The tick (async: Telegram sends) ------------------------------------------


@dataclass
class TickResult:
    new_days: list[date]
    events: int
    queued: int
    sent_users: int
    failed_users: int
    no_data_alert: bool = False


async def _db(fn, *args):
    def call():
        with new_session() as s:
            return fn(s, *args)

    return await asyncio.to_thread(call)


def _load_and_queue(session: Session, source: MarketDataSource, today: date) -> tuple[list[date], int, int]:
    start_run(session, today)
    new_days = ingest_new_days(session, source, today)
    event_ids = evaluate(session, watched_symbols(session)) if new_days else []
    queued = queue_deliveries(session, event_ids)
    session.commit()
    if session.get(TradingDay, today) is not None:
        finish_run(session, today, RunStatus.DONE)
    return new_days, len(event_ids), queued


async def send_pending(bot: Bot) -> tuple[int, int]:
    """Send every pending digest. Returns (users sent, users failed)."""
    sent = failed = 0
    for user_id, box in (await _db(pending_outboxes)).items():
        try:
            for chunk in messages.digest(box.alerts):
                await bot.send_message(box.chat_id, chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Forbidden as exc:  # user blocked the bot or deleted their account
            log.warning("user %s unreachable: %s", user_id, exc)
            await _db(mark, box.deliveries, DeliveryStatus.SKIPPED, str(exc))
            failed += 1
        except TelegramError as exc:
            log.warning("sending to user %s failed: %s", user_id, exc)
            await _db(mark, box.deliveries, DeliveryStatus.FAILED, str(exc))
            failed += 1
        else:
            await _db(mark, box.deliveries, DeliveryStatus.SENT)
            sent += 1
    return sent, failed


async def tick(bot: Bot, source: MarketDataSource, now: datetime, admin_id: int | None) -> TickResult:
    today = now.astimezone(IST).date()
    try:
        new_days, events, queued = await _db(_load_and_queue, source, today)
    except Exception as exc:
        log.exception("daily run failed")
        if await _db(finish_run, today, RunStatus.FAILED, repr(exc)) and admin_id:
            await bot.send_message(admin_id, messages.run_failed(today, exc), parse_mode=ParseMode.HTML)
        raise
    sent, failed = await send_pending(bot)
    result = TickResult(new_days, events, queued, sent, failed)

    # Last tick of the evening on a weekday and still no file: tell the admin once.
    if now.astimezone(IST).time() >= WINDOW_END and today.weekday() < 5 and admin_id:
        if await _db(_run_status, today) == RunStatus.RUNNING:
            await _db(finish_run, today, RunStatus.NO_DATA)
            await bot.send_message(admin_id, messages.no_data(today), parse_mode=ParseMode.HTML)
            result.no_data_alert = True
    log.info("tick %s: %s", now.astimezone(IST).strftime("%Y-%m-%d %H:%M"), result)
    return result


def _run_status(session: Session, day: date) -> RunStatus | None:
    run = session.get(Run, day)
    return run.status if run else None


def in_window(now: datetime) -> bool:
    return WINDOW_START <= now.astimezone(IST).time() <= WINDOW_END
