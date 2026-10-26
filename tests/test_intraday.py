import asyncio
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from bhavcopy_fixture import isin
from test_daily import ADMIN, FRIEND, FakeBot, FakeSource, at, history, setup_users
from tradio import daily, intraday, messages
from tradio.alerts import alert_item
from tradio.config import IST
from tradio.data.upstox import Candle, MarketHours, Quote
from tradio.models import Alert, AlertKind, CandlePrice, CandleProgress, Delivery, Direction, SmaState


class FakeLive:
    def __init__(self, today, prices=None, prev_close="100", closed=()):
        self.today = today
        self.prices = prices or {}
        self.prev_close = prev_close
        self.closed = set(closed)
        self.calls = []
        self.missing = set()

    def market_hours(self, day):
        if day in self.closed:
            return None
        return MarketHours(at(day, 9, 15), at(day, 15, 30))

    def quotes(self, isins):
        return {key: Quote(Decimal(99999), Decimal(self.prev_close)) for key in isins}

    def candles(self, key, day, start=None):
        self.calls.append((key, day, start))
        days = [self.today] if start is None else [day]
        candles = []
        for d in days:
            for i in range(25):
                stamp = at(d, 9, 15) + i * intraday.INTERVAL
                if stamp not in self.missing:
                    candles.append(Candle(stamp, Decimal(self.prices.get((key, stamp), "100"))))
        return list(reversed(candles))  # provider returns newest first


def run(bot, live, now, nse=None):
    return asyncio.run(intraday.tick(bot, live, nse or FakeSource({}), now, ADMIN))


def closed_at(day, hh, mm):
    return at(day, hh, mm) + intraday.PUBLISH_DELAY


def test_schedule_and_last_candle():
    day = date(2026, 10, 23)
    hours = FakeLive(day).market_hours(day)
    assert intraday.next_check(at(day, 9, 29)) == closed_at(day, 9, 30)
    assert intraday.next_check(at(day, 9, 30)) == closed_at(day, 9, 30)
    assert intraday.next_check(closed_at(day, 9, 30)) == closed_at(day, 9, 45)
    assert not intraday.in_hours(hours, at(day, 9, 30))
    assert intraday.in_hours(hours, closed_at(day, 9, 30))
    assert intraday.in_hours(hours, closed_at(day, 15, 30))
    live = FakeLive(day)
    assert intraday.expected_starts(live, day, at(day, 9, 30)) == []
    assert intraday.expected_starts(live, day, closed_at(day, 9, 30)) == [at(day, 9, 15)]
    assert len(intraday.expected_starts(live, day, closed_at(day, 15, 30))) == 25
    live.closed.add(day)
    assert intraday.expected_starts(live, day, closed_at(day, 15, 30)) == []


def test_sma_uses_candles_and_resets_after_missing_bar():
    start = at(date(2026, 10, 23), 9, 15)
    calendar = [start + i * intraday.INTERVAL for i in range(50)]
    candles = [Candle(stamp, Decimal(i + 1)) for i, stamp in enumerate(calendar)]
    points = intraday.evaluate_candles(candles, calendar)
    first = points[0]
    assert (first.sma8, first.sma13, first.sma21) == (Decimal("17.5"), Decimal(15), Decimal(11))
    assert first.direction is None
    assert first.state == SmaState.ABOVE
    # Missing expected candle resets the indicator, rather than averaging over it.
    points = intraday.evaluate_candles(candles[:23] + candles[24:], calendar)
    assert [p.start for p in points if calendar[24] <= p.start < calendar[44]] == []
    assert next(p for p in points if p.start == calendar[44]).direction is None


def test_closed_candle_cross_is_durable_and_open_candle_is_ignored(db):
    today = history(db)
    setup_users(db)
    live = FakeLive(today, {(isin("TCS"), at(today, 9, 15)): "120",
                            (isin("TCS"), at(today, 9, 30)): "1"})
    bot = FakeBot()
    ids = run(bot, live, closed_at(today, 9, 30))
    assert len(ids) == 1
    assert sorted(chat for chat, _ in bot.sent) == [ADMIN, FRIEND]
    text = bot.sent[0][1]
    assert "TCS · bullish cross · 15m" in text
    assert "09:15–09:30 IST" in text
    assert "Close ₹120.00" in text and "Provisional" not in text
    assert "interval=15" in text
    assert db.scalar(select(func.count()).select_from(CandlePrice).where(CandlePrice.start == at(today, 9, 30))) == 0
    alert = db.get(Alert, ids[0])
    assert alert.candle_start == at(today, 9, 15)
    assert db.get(CandleProgress, "TCS").last_candle == at(today, 9, 15)
    bot.sent.clear()
    # A new session/process uses persisted progress; no alert repeats.
    db.expire_all()
    assert run(bot, live, closed_at(today, 9, 30)) == []
    assert bot.sent == []
    assert len([call for call in live.calls if call[2] is not None]) == 2  # once per stock


def test_reverse_cross_is_a_new_signal_and_close_is_separate(db):
    today = history(db)
    setup_users(db)
    live = FakeLive(today, {(isin("TCS"), at(today, 9, 15)): "120",
                            (isin("TCS"), at(today, 9, 30)): "1"})
    bot = FakeBot()
    run(bot, live, closed_at(today, 9, 30))
    run(bot, live, closed_at(today, 9, 45))
    alerts = db.scalars(select(Alert).order_by(Alert.id)).all()
    assert [(a.kind, a.direction) for a in alerts] == [(AlertKind.CANDLE, Direction.BULLISH), (AlertKind.CANDLE, Direction.BEARISH)]
    assert "cross undone" not in bot.sent[-1][1]
    bot.sent.clear()
    asyncio.run(daily.tick(bot, FakeSource({today: {"TCS": "100", "INFY": "100"}}), at(today), ADMIN))
    assert bot.sent == []  # daily flat close doesn't cancel the 15-minute signal


def test_restart_catches_up_multiple_candles_and_final_candle(db):
    today = history(db)
    setup_users(db)
    live = FakeLive(today)
    bot = FakeBot()
    run(bot, live, closed_at(today, 9, 30))  # seed a flat state
    live.prices[(isin("TCS"), at(today, 9, 30))] = "120"
    live.prices[(isin("TCS"), at(today, 9, 45))] = "1"
    ids = run(bot, live, closed_at(today, 10, 0))
    assert len(ids) == 2
    assert [a.direction for a in db.scalars(select(Alert).order_by(Alert.id))] == [Direction.BULLISH, Direction.BEARISH]
    digest = messages.digest([alert_item(db.get(Alert, i), "TCS") for i in ids])[0]
    assert digest.index("bullish") < digest.index("bearish")
    run(bot, live, closed_at(today, 15, 30))
    assert db.get(CandleProgress, "TCS").last_candle == at(today, 15, 15)


def test_dry_run_writes_nothing(db):
    today = history(db)
    setup_users(db)
    results = intraday.evaluate_live(db, FakeLive(today), closed_at(today, 9, 30), store=False)
    assert len(results) == 2
    for model in (CandlePrice, CandleProgress, Alert, Delivery):
        assert db.scalar(select(func.count()).select_from(model)) == 0


def test_split_resets_window_instead_of_firing_fake_cross(db):
    today = history(db)
    setup_users(db)
    bot = FakeBot()
    live = FakeLive(today, prev_close="50")
    live.prices = {(isin(s), at(today, 9, 15) + i * intraday.INTERVAL): "50" for s in ("TCS", "INFY") for i in range(25)}
    assert run(bot, live, closed_at(today, 9, 30)) == []
    assert bot.sent == []
    assert run(bot, live, closed_at(today, 15, 30)) == []


def test_stale_history_pauses_and_notifies_once(db):
    yesterday = history(db)
    today = yesterday + timedelta(days=1)
    setup_users(db)
    bot = FakeBot()
    live = FakeLive(today)
    assert run(bot, live, closed_at(today, 10, 0)) is None
    assert run(bot, live, closed_at(today, 10, 15)) is None
    assert [chat for chat, _ in bot.sent] == [ADMIN]


def test_missed_evening_run_catches_up_before_candle_check(db):
    yesterday = history(db)
    today = yesterday + timedelta(days=1)
    setup_users(db)
    live = FakeLive(today, {(isin("TCS"), at(today, 9, 15)): "120"})
    ids = run(FakeBot(), live, closed_at(today, 9, 30), FakeSource({yesterday: {"TCS": "100", "INFY": "100"}}))
    assert len(ids) == 1


def test_token_expiry_reminders_go_out_once_each(db):
    bot = FakeBot()
    expiry = datetime(2027, 10, 19, 3, 30, tzinfo=IST)
    def remind(days):
        return asyncio.run(intraday.remind_expiry(bot, expiry, expiry - timedelta(days=days), ADMIN))
    assert not remind(40)
    assert remind(25)
    assert not remind(24)
    assert remind(5)
    assert [chat for chat, _ in bot.sent] == [ADMIN, ADMIN]


def test_reliance_23_october_1400_matches_shared_chart():
    # Upstox NSE_EQ|INE002A01018, retrieved 26 Oct 2026. Last 22 completed
    # 15-minute candles ending at the screenshot's selected 14:00 candle.
    day = date(2026, 10, 23)
    calendar = [at(day - timedelta(days=1), 15, 0), at(day - timedelta(days=1), 15, 15)]
    calendar += [at(day, 9, 15) + i * intraday.INTERVAL for i in range(20)]
    closes = "1189.9 1184.5 1198.5 1192.4 1190.0 1178.6 1175.5 1172.7 1168.2 1171.7 1170.3 1166.7 1165.0 1167.3 1169.1 1173.6 1173.3 1173.1 1174.8 1186.1 1183.2 1186.8".split()
    points = intraday.evaluate_candles([Candle(t, Decimal(c)) for t, c in zip(calendar, closes, strict=True)], calendar)
    previous, signal = points
    assert previous.state != SmaState.ABOVE
    assert signal.start == at(day, 14, 0)
    assert signal.close == Decimal("1186.8")
    assert signal.sma8 == Decimal("1177.5")
    assert signal.sma13.quantize(Decimal("0.01")) == Decimal("1173.92")
    assert signal.sma21.quantize(Decimal("0.01")) == Decimal("1177.21")
    assert signal.direction == Direction.BULLISH
    # SMA13 remains BELOW SMA21: a full 8 > 13 > 21 stack isn't required.
    assert signal.sma13 < signal.sma21


def test_special_session_calendar_uses_session_open():
    day = date(2026, 10, 23)
    class Special(FakeLive):
        def market_hours(self, day):
            return MarketHours(at(day, 18, 5), at(day, 19, 5))
    live = Special(day)
    assert intraday.expected_starts(live, day, closed_at(day, 18, 35)) == [at(day, 18, 5), at(day, 18, 20)]
    assert intraday.next_check(at(day, 18, 10), live.market_hours(day)) == closed_at(day, 18, 20)
    assert intraday.next_check(closed_at(day, 18, 20), live.market_hours(day)) == closed_at(day, 18, 35)


def test_first_run_does_not_alert_an_old_candle_when_provider_is_late(db):
    today = history(db)
    setup_users(db)
    live = FakeLive(today, {(isin("TCS"), at(today, 9, 15)): "120"})
    live.missing = {at(today, 9, 30)}
    assert run(FakeBot(), live, closed_at(today, 9, 45)) == []
    # The late candle can be picked up on the next tick without replaying the old buy.
    live.missing.clear()
    assert run(FakeBot(), live, closed_at(today, 10, 0)) == []
