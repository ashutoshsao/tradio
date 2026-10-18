"""Alerts to users: one delivery per watcher, sent as one digest per user.

Shared by the intraday and evening runs. See docs/v2-spec.md §4.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError

from tradio import messages
from tradio.config import IST
from tradio.db import in_session
from tradio.engine import trading_calendar
from tradio.models import AdminNotice, Alert, Delivery, DeliveryStatus, Instrument, User, WatchlistEntry
from tradio.watchlist import AlertItem

log = logging.getLogger(__name__)

MAX_ALERT_AGE = 5  # trading days; older crosses (e.g. after a long laptop sleep) aren't sent
MAX_ATTEMPTS = 5


def watched_symbols(session: Session) -> list[str]:
    return list(
        session.scalars(
            select(WatchlistEntry.symbol)
            .join(User, User.telegram_user_id == WatchlistEntry.user_id)
            .where(User.is_allowed)
            .distinct()
        )
    )


def add_alert(session: Session, **values) -> int | None:
    """Insert an alert; None if the same one exists (a rerun)."""
    return session.scalar(insert(Alert).values(**values).on_conflict_do_nothing().returning(Alert.id))


def queue_deliveries(session: Session, alert_ids: list[int]) -> int:
    """One pending delivery per (new alert, allowed user watching it), for recent alerts only."""
    if not alert_ids:
        return 0
    calendar = trading_calendar(session)
    oldest = calendar[-MAX_ALERT_AGE] if len(calendar) >= MAX_ALERT_AGE else date.min
    pairs = session.execute(
        select(Alert.id, WatchlistEntry.user_id)
        .join(WatchlistEntry, WatchlistEntry.symbol == Alert.symbol)
        .join(User, User.telegram_user_id == WatchlistEntry.user_id)
        .where(Alert.id.in_(alert_ids), Alert.trade_date >= oldest, User.is_allowed)
    ).all()
    if pairs:
        session.execute(
            insert(Delivery).values([{"alert_id": a, "user_id": u} for a, u in pairs]).on_conflict_do_nothing()
        )
    return len(pairs)


def alert_item(a: Alert, name: str) -> AlertItem:
    return AlertItem(
        a.symbol, name, a.trade_date, a.direction, a.price, a.sma8, a.sma13, a.sma21,
        kind=a.kind, seq=a.seq, after_intraday=a.after_intraday, at=a.created_at.astimezone(IST),
    )


@dataclass
class Outbox:
    chat_id: int
    deliveries: list[tuple[int, int]] = field(default_factory=list)  # (alert_id, user_id)
    alerts: list[AlertItem] = field(default_factory=list)


def pending_outboxes(session: Session) -> dict[int, Outbox]:
    rows = session.execute(
        select(Delivery, Alert, Instrument.name, User.chat_id)
        .join(Alert, Alert.id == Delivery.alert_id)
        .join(Instrument, Instrument.symbol == Alert.symbol)
        .join(User, User.telegram_user_id == Delivery.user_id)
        .where(
            Delivery.status.in_([DeliveryStatus.PENDING, DeliveryStatus.FAILED]),
            Delivery.attempts < MAX_ATTEMPTS,
            User.is_allowed,
        )
        .order_by(Alert.id)
    )
    boxes: dict[int, Outbox] = {}
    for d, a, name, chat_id in rows:
        box = boxes.setdefault(d.user_id, Outbox(chat_id))
        box.deliveries.append((d.alert_id, d.user_id))
        box.alerts.append(alert_item(a, name))
    return boxes


def mark(session: Session, keys: list[tuple[int, int]], status: DeliveryStatus, error: str | None = None) -> None:
    for alert_id, user_id in keys:
        d = session.get(Delivery, (alert_id, user_id))
        d.status = status
        d.attempts += 1
        d.error = error
        if status == DeliveryStatus.SENT:
            d.sent_at = datetime.now(IST)
    session.commit()


async def send_pending(bot: Bot) -> tuple[int, int]:
    """Send every pending digest. Returns (users sent, users failed)."""
    sent = failed = 0
    for user_id, box in (await in_session(pending_outboxes)).items():
        try:
            for chunk in messages.digest(box.alerts):
                await bot.send_message(box.chat_id, chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Forbidden as exc:  # user blocked the bot or deleted their account
            log.warning("user %s unreachable: %s", user_id, exc)
            await in_session(mark, box.deliveries, DeliveryStatus.SKIPPED, str(exc))
            failed += 1
        except TelegramError as exc:
            log.warning("sending to user %s failed: %s", user_id, exc)
            await in_session(mark, box.deliveries, DeliveryStatus.FAILED, str(exc))
            failed += 1
        else:
            await in_session(mark, box.deliveries, DeliveryStatus.SENT)
            sent += 1
    return sent, failed


def notify_once(session: Session, *keys: str) -> bool:
    """Record admin notices; True if any of `keys` is new (so the message should go out)."""
    new = session.scalars(
        insert(AdminNotice).values([{"key": k} for k in keys]).on_conflict_do_nothing().returning(AdminNotice.key)
    ).all()
    session.commit()
    return bool(new)
