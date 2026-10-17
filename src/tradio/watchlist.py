"""Users, access control and watchlists. See docs/v1-spec.md §7.

Plain functions over a Session so they can be tested without Telegram.
"""

import difflib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from tradio.adjustments import adjusted_closes
from tradio.engine import evaluate, trading_calendar
from tradio.models import Direction, Instrument, SignalEvent, SignalState, SmaState, TradingDay, User, WatchlistEntry
from tradio.signals import SLOW, split_on_gaps

MAX_WATCHLIST = 50


def register(session: Session, user_id: int, chat_id: int, username: str | None, admin_id: int | None) -> User:
    """Create or refresh a user. The configured admin is always allowed."""
    user = session.get(User, user_id)
    if user is None:
        user = User(telegram_user_id=user_id, chat_id=chat_id, is_admin=False, is_allowed=False)
        session.add(user)
    user.chat_id = chat_id
    user.username = username
    if user_id == admin_id:
        user.is_admin = user.is_allowed = True
    session.commit()
    return user


def set_allowed(session: Session, user_id: int, allowed: bool) -> User | None:
    user = session.get(User, user_id)
    if user is None or user.is_admin:
        return None
    user.is_allowed = allowed
    session.commit()
    return user


def list_users(session: Session) -> list[tuple[User, int]]:
    counts = func.count(WatchlistEntry.symbol)
    return list(
        session.execute(
            select(User, counts)
            .outerjoin(WatchlistEntry, WatchlistEntry.user_id == User.telegram_user_id)
            .group_by(User.telegram_user_id)
            .order_by(User.created_at)
        ).tuples()
    )


def latest_trading_day(session: Session) -> date | None:
    return session.scalar(select(func.max(TradingDay.trade_date)))


@dataclass(frozen=True)
class StockStatus:
    symbol: str
    as_of: date | None  # latest computed state; None while history is too short
    state: SmaState | None
    close: Decimal | None
    sma8: Decimal | None
    sma13: Decimal | None
    sma21: Decimal | None
    last_event: tuple[date, Direction] | None
    days_until_ready: int  # trading days until a cross can fire (needs SLOW + 1 continuous closes)
    last_seen: date | None  # last day the stock traded in the EQ series


def stock_status(session: Session, symbol: str) -> StockStatus:
    state = session.scalar(
        select(SignalState).where(SignalState.symbol == symbol).order_by(SignalState.trade_date.desc()).limit(1)
    )
    event = session.execute(
        select(SignalEvent.trade_date, SignalEvent.direction)
        .where(SignalEvent.symbol == symbol)
        .order_by(SignalEvent.trade_date.desc())
        .limit(1)
    ).first()
    segments = split_on_gaps(adjusted_closes(session, [symbol]).get(symbol, []), trading_calendar(session))
    latest_run = segments[-1] if segments else []
    instrument = session.get(Instrument, symbol)

    current = bool(state and latest_run and state.trade_date == latest_run[-1][0])
    return StockStatus(
        symbol=symbol,
        as_of=state.trade_date if current else None,
        state=state.state if current else None,
        close=latest_run[-1][1] if current else None,
        sma8=state.sma8 if current else None,
        sma13=state.sma13 if current else None,
        sma21=state.sma21 if current else None,
        last_event=tuple(event) if event else None,
        days_until_ready=max(0, SLOW + 1 - len(latest_run)),
        last_seen=instrument.last_seen_date if instrument else None,
    )


@dataclass(frozen=True)
class AddResult:
    added: list[StockStatus]
    already: list[str]
    unknown: dict[str, list[str]]  # symbol -> suggestions
    full: list[str]  # rejected because the watchlist is at MAX_WATCHLIST


def normalise(symbol: str) -> str:
    return symbol.strip().upper().removesuffix(".NS")


def add_symbols(session: Session, user_id: int, symbols: list[str]) -> AddResult:
    """Watch EQ stocks traded on the latest trading day. Past signals are not sent, only future ones."""
    latest = latest_trading_day(session)
    tradeable = set(session.scalars(select(Instrument.symbol).where(Instrument.last_seen_date == latest)))
    watched = set(session.scalars(select(WatchlistEntry.symbol).where(WatchlistEntry.user_id == user_id)))

    added, already, unknown, full = [], [], {}, []
    for symbol in dict.fromkeys(normalise(s) for s in symbols if s.strip()):
        if symbol in watched:
            already.append(symbol)
        elif symbol not in tradeable:
            unknown[symbol] = difflib.get_close_matches(symbol, tradeable, n=3, cutoff=0.6)
        elif len(watched) >= MAX_WATCHLIST:
            full.append(symbol)
        else:
            session.add(WatchlistEntry(user_id=user_id, symbol=symbol))
            watched.add(symbol)
            added.append(symbol)
    if added:
        # Fill in state history so /add can show where the stock stands now.
        evaluate(session, added)
    session.commit()
    return AddResult([stock_status(session, s) for s in added], already, unknown, full)


def remove_symbols(session: Session, user_id: int, symbols: list[str]) -> tuple[list[str], list[str]]:
    """Returns (removed, not watched)."""
    wanted = [normalise(s) for s in symbols if s.strip()]
    removed = list(
        session.scalars(
            delete(WatchlistEntry)
            .where(WatchlistEntry.user_id == user_id, WatchlistEntry.symbol.in_(wanted))
            .returning(WatchlistEntry.symbol)
        )
    )
    session.commit()
    return removed, [s for s in wanted if s not in removed]


def watchlist(session: Session, user_id: int) -> list[StockStatus]:
    symbols = session.scalars(
        select(WatchlistEntry.symbol).where(WatchlistEntry.user_id == user_id).order_by(WatchlistEntry.symbol)
    )
    return [stock_status(session, s) for s in symbols]
