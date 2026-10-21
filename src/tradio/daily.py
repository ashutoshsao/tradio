"""The evening run: load NSE's official closes, confirm or cancel the day's crosses, send digests.

See docs/v1-spec.md §6 and docs/v2-spec.md §4. Every tick is idempotent, so it
can run every 15 minutes from the bot's job queue or as a Kubernetes CronJob.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from telegram import Bot
from telegram.constants import ParseMode

from tradio import messages
from tradio.alerts import add_alert, queue_deliveries, send_pending, watched_symbols
from tradio.config import IST
from tradio.data.base import MarketDataSource
from tradio.data.upstox import LivePriceSource
from tradio.db import in_session
from tradio.engine import evaluate
from tradio.ingest import ingest_day, sync_actions_api
from tradio.models import AlertKind, DailyPrice, IntradaySignal, Run, RunStatus, SignalEvent, SignalState, TradingDay
from tradio.signals import MAX_CROSSES

log = logging.getLogger(__name__)

WINDOW_START, WINDOW_END = time(18, 0), time(22, 0)
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


def close_alerts(session: Session, event_ids: list[int], days: list[date]) -> list[int]:
    """Confirmed / not-confirmed alerts for newly loaded days. Returns new alert ids."""
    events = session.scalars(select(SignalEvent).where(SignalEvent.id.in_(event_ids))).all()
    intraday = {
        (r.symbol, r.trade_date): r
        for r in session.scalars(select(IntradaySignal).where(IntradaySignal.trade_date.in_(days)))
    }
    ids = []
    for e in events:
        row = intraday.pop((e.symbol, e.trade_date), None)
        ids.append(
            add_alert(
                session, symbol=e.symbol, trade_date=e.trade_date, kind=AlertKind.CONFIRMED,
                direction=e.direction, price=e.close, sma8=e.sma8, sma13=e.sma13, sma21=e.sma21,
                after_intraday=row is not None and row.direction == e.direction,
            )
        )
    for (symbol, day), row in intraday.items():
        # An undone cross was already reported; only a cross users last saw as live needs cancelling.
        if not (row.active or row.crosses >= MAX_CROSSES):
            continue
        state = session.get(SignalState, (symbol, day))
        close = session.get(DailyPrice, (symbol, day))
        ids.append(
            add_alert(
                session, symbol=symbol, trade_date=day, kind=AlertKind.NOT_CONFIRMED, direction=row.direction,
                price=close.close if close else row.price,
                sma8=state.sma8 if state else row.sma8,
                sma13=state.sma13 if state else row.sma13,
                sma21=state.sma21 if state else row.sma21,
            )
        )
    return [i for i in ids if i is not None]


def load_and_alert(session: Session, source: MarketDataSource, today: date) -> tuple[list[date], int, int]:
    """Load any new trading days, evaluate watched stocks, queue alerts. Returns (new days, events, deliveries)."""
    new_days = ingest_new_days(session, source, today)
    event_ids = evaluate(session, watched_symbols(session)) if new_days else []
    queued = queue_deliveries(session, close_alerts(session, event_ids, new_days)) if new_days else 0
    session.commit()
    return new_days, len(event_ids), queued


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


def _run_and_alert(session: Session, source: MarketDataSource, today: date) -> tuple[list[date], int, int]:
    start_run(session, today)
    result = load_and_alert(session, source, today)
    if session.get(TradingDay, today) is not None:
        finish_run(session, today, RunStatus.DONE)
    return result


async def tick(
    bot: Bot, source: MarketDataSource, now: datetime, admin_id: int | None, live: LivePriceSource | None = None
) -> TickResult:
    today = now.astimezone(IST).date()
    try:
        new_days, events, queued = await in_session(_run_and_alert, source, today)
    except Exception as exc:
        log.exception("daily run failed")
        if await in_session(finish_run, today, RunStatus.FAILED, repr(exc)) and admin_id:
            await bot.send_message(admin_id, messages.run_failed(today, exc), parse_mode=ParseMode.HTML)
        raise
    sent, failed = await send_pending(bot)
    result = TickResult(new_days, events, queued, sent, failed)

    # Last tick of the evening and still no file: a closed market is recorded quietly,
    # a trading day without a file is told to the admin once.
    if _minute(now) >= WINDOW_END and await in_session(_run_status, today) == RunStatus.RUNNING:
        if await asyncio.to_thread(market_closed, live, today):
            await in_session(finish_run, today, RunStatus.CLOSED)
        elif admin_id:
            await in_session(finish_run, today, RunStatus.NO_DATA)
            await bot.send_message(admin_id, messages.no_data(today), parse_mode=ParseMode.HTML)
            result.no_data_alert = True
    log.info("tick %s: %s", now.astimezone(IST).strftime("%Y-%m-%d %H:%M"), result)
    return result


def _run_status(session: Session, day: date) -> RunStatus | None:
    run = session.get(Run, day)
    return run.status if run else None


def market_closed(live: LivePriceSource | None, day: date) -> bool:
    """Upstox's calendar if available (knows holidays and weekend special sessions), else weekends."""
    if live is not None:
        try:
            return live.market_hours(day) is None
        except Exception:
            log.warning("Upstox market hours unavailable for %s; assuming weekdays trade", day, exc_info=True)
    return day.weekday() >= 5


def _minute(now: datetime) -> time:
    # To the minute: the 15-minute job fires a few milliseconds late, and 22:00:00.4 is the 22:00 tick.
    return now.astimezone(IST).replace(second=0, microsecond=0).time()


def in_window(now: datetime) -> bool:
    return WINDOW_START <= _minute(now) <= WINDOW_END
