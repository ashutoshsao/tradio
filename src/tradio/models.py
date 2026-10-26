"""Database schema. See docs/v1-spec.md §5 and docs/v2-spec.md §4."""

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

Price = Numeric(12, 2)
Sma = Numeric(14, 4)


class Base(DeclarativeBase):
    type_annotation_map = {datetime: DateTime(timezone=True)}
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


def _enum(cls: type[enum.Enum]) -> Enum:
    # Stored as VARCHAR so adding a value never needs an ALTER TYPE migration.
    return Enum(cls, native_enum=False, length=16, values_callable=lambda e: [m.value for m in e])


class SmaState(enum.StrEnum):
    ABOVE = "above"
    BELOW = "below"
    MIXED = "mixed"


class Direction(enum.StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"


class DeliveryStatus(enum.StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class AlertKind(enum.StrEnum):
    CANDLE = "candle"  # confirmed 15-minute candle crossover
    CROSS = "cross"  # intraday, provisional
    UNDONE = "undone"  # intraday: an alerted cross no longer holds
    CONFIRMED = "confirmed"  # official close
    NOT_CONFIRMED = "not_confirmed"  # official close: an alerted cross didn't hold


class RunStatus(enum.StrEnum):
    RUNNING = "running"
    DONE = "done"
    NO_DATA = "no_data"  # a trading day, but no file by the end of the evening
    CLOSED = "closed"  # NSE was closed (weekend or holiday): no file expected
    FAILED = "failed"


class Instrument(Base):
    __tablename__ = "instruments"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    isin: Mapped[str] = mapped_column(String(12))
    name: Mapped[str] = mapped_column(String(128))
    series: Mapped[str] = mapped_column(String(4))
    last_seen_date: Mapped[date]


class DailyPrice(Base):
    """Raw bhavcopy prices, never modified. Adjustments are applied on read."""

    __tablename__ = "daily_prices"

    symbol: Mapped[str] = mapped_column(ForeignKey("instruments.symbol"), primary_key=True)
    trade_date: Mapped[date] = mapped_column(primary_key=True, index=True)
    open: Mapped[Decimal] = mapped_column(Price)
    high: Mapped[Decimal] = mapped_column(Price)
    low: Mapped[Decimal] = mapped_column(Price)
    close: Mapped[Decimal] = mapped_column(Price)
    prev_close: Mapped[Decimal] = mapped_column(Price)
    volume: Mapped[int] = mapped_column(BigInteger)


class TradingDay(Base):
    """A day NSE published a bhavcopy, and whether its corporate actions are loaded."""

    __tablename__ = "trading_days"

    trade_date: Mapped[date] = mapped_column(primary_key=True)
    actions_loaded: Mapped[bool] = mapped_column(default=False)


class PriceAdjustment(Base):
    """One corporate action: prices before ex_date are multiplied by factor.

    `action` is a normalised key ("bonus 4:1", "split 2>1", "demerger") so the
    same action reported by two NSE sources is stored once. Several actions on
    one ex-date (e.g. bonus + split) multiply. No FK to instruments: NSE lists
    actions for symbols we may not have seen yet.
    """

    __tablename__ = "price_adjustments"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    ex_date: Mapped[date] = mapped_column(primary_key=True)
    action: Mapped[str] = mapped_column(String(32), primary_key=True)
    factor: Mapped[Decimal] = mapped_column(Numeric(16, 10))
    purpose: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(8))
    detected_at: Mapped[datetime] = mapped_column(server_default=func.now())


class User(Base):
    __tablename__ = "users"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(String(64))
    is_admin: Mapped[bool] = mapped_column(default=False)
    is_allowed: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class WatchlistEntry(Base):
    __tablename__ = "watchlist"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"), primary_key=True
    )
    symbol: Mapped[str] = mapped_column(ForeignKey("instruments.symbol"), primary_key=True, index=True)
    added_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SignalState(Base):
    __tablename__ = "signal_states"

    symbol: Mapped[str] = mapped_column(ForeignKey("instruments.symbol"), primary_key=True)
    trade_date: Mapped[date] = mapped_column(primary_key=True)
    sma8: Mapped[Decimal] = mapped_column(Sma)
    sma13: Mapped[Decimal] = mapped_column(Sma)
    sma21: Mapped[Decimal] = mapped_column(Sma)
    state: Mapped[SmaState] = mapped_column(_enum(SmaState))


class SignalEvent(Base):
    __tablename__ = "signal_events"
    __table_args__ = (UniqueConstraint("symbol", "trade_date", "direction"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(ForeignKey("instruments.symbol"))
    trade_date: Mapped[date] = mapped_column(index=True)
    direction: Mapped[Direction] = mapped_column(_enum(Direction))
    close: Mapped[Decimal] = mapped_column(Price)
    sma8: Mapped[Decimal] = mapped_column(Sma)
    sma13: Mapped[Decimal] = mapped_column(Sma)
    sma21: Mapped[Decimal] = mapped_column(Sma)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class IntradaySignal(Base):
    """A stock's provisional cross during one trading day. See docs/v2-spec.md §4."""

    __tablename__ = "intraday_signals"

    symbol: Mapped[str] = mapped_column(ForeignKey("instruments.symbol"), primary_key=True)
    trade_date: Mapped[date] = mapped_column(primary_key=True)
    direction: Mapped[Direction] = mapped_column(_enum(Direction))
    active: Mapped[bool]  # crossed as of the latest tick
    crosses: Mapped[int]  # times it crossed today (cross alerts are capped)
    price: Mapped[Decimal] = mapped_column(Price)
    sma8: Mapped[Decimal] = mapped_column(Sma)
    sma13: Mapped[Decimal] = mapped_column(Sma)
    sma21: Mapped[Decimal] = mapped_column(Sma)
    updated_at: Mapped[datetime]


class CandlePrice(Base):
    """Raw completed 15-minute closes; retained across pod restarts."""

    __tablename__ = "candle_prices"
    symbol: Mapped[str] = mapped_column(ForeignKey("instruments.symbol"), primary_key=True)
    start: Mapped[datetime] = mapped_column(primary_key=True)
    close: Mapped[Decimal] = mapped_column(Price)


class CandleProgress(Base):
    __tablename__ = "candle_progress"
    symbol: Mapped[str] = mapped_column(ForeignKey("instruments.symbol"), primary_key=True)
    last_candle: Mapped[datetime | None]
    last_direction: Mapped[Direction | None] = mapped_column(_enum(Direction))
    history_through: Mapped[date | None]


class Alert(Base):
    """One user-facing notification about a stock, sent to everyone watching it."""

    __tablename__ = "alerts"
    __table_args__ = (UniqueConstraint("symbol", "trade_date", "kind", "seq"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(ForeignKey("instruments.symbol"))
    trade_date: Mapped[date] = mapped_column(index=True)
    kind: Mapped[AlertKind] = mapped_column(_enum(AlertKind))
    seq: Mapped[int] = mapped_column(default=0)  # which intraday cross this is about; 0 for the close
    direction: Mapped[Direction] = mapped_column(_enum(Direction))
    price: Mapped[Decimal] = mapped_column(Price)
    sma8: Mapped[Decimal] = mapped_column(Sma)
    sma13: Mapped[Decimal] = mapped_column(Sma)
    sma21: Mapped[Decimal] = mapped_column(Sma)
    after_intraday: Mapped[bool] = mapped_column(default=False)  # confirmed: users already had a cross alert
    candle_start: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Delivery(Base):
    __tablename__ = "deliveries"

    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[DeliveryStatus] = mapped_column(
        _enum(DeliveryStatus), default=DeliveryStatus.PENDING, index=True
    )
    attempts: Mapped[int] = mapped_column(default=0)
    sent_at: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(Text)


class AdminNotice(Base):
    """Admin messages that must go out once (e.g. "token expires in 7 days")."""

    __tablename__ = "admin_notices"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Run(Base):
    __tablename__ = "runs"

    trade_date: Mapped[date] = mapped_column(primary_key=True)
    status: Mapped[RunStatus] = mapped_column(_enum(RunStatus))
    attempts: Mapped[int] = mapped_column(default=0)
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(Text)
