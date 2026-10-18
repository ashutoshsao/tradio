import asyncio
from datetime import datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select

from bhavcopy_fixture import isin
from test_daily import ADMIN, FRIEND, FakeBot, FakeSource, at, history, setup_users
from tradio import daily, intraday
from tradio.config import IST
from tradio.data.upstox import MarketHours, Quote
from tradio.models import Alert, AlertKind, IntradaySignal


class FakeLive:
    """Upstox stand-in: every day is a 9:15–15:30 session unless listed as closed."""

    def __init__(self, prices: dict[str, str], prev_close: dict[str, str] | None = None, closed=()):
        self.prices = prices
        self.prev_close = prev_close or {}
        self.closed = set(closed)

    def quotes(self, isins):
        return {
            isin(s): Quote(Decimal(p), Decimal(self.prev_close.get(s, "100")))
            for s, p in self.prices.items()
            if isin(s) in isins
        }

    def market_hours(self, day):
        if day in self.closed:
            return None
        return MarketHours(datetime.combine(day, time(9, 15), IST), datetime.combine(day, time(15, 30), IST))


def run(bot, live, now, nse=None):
    return asyncio.run(intraday.tick(bot, live, nse or FakeSource({}), now, ADMIN))


def evening(bot, day, closes):
    return asyncio.run(daily.tick(bot, FakeSource({day: closes}), at(day, 19, 0), ADMIN))


def kinds(db):
    return [(a.symbol, a.kind, a.seq) for a in db.scalars(select(Alert).order_by(Alert.id))]


def test_live_cross_is_alerted_once_as_provisional(db):
    today = history(db)  # 30 flat days at 100
    setup_users(db)
    bot = FakeBot()

    run(bot, FakeLive({"TCS": "120", "INFY": "100"}), at(today, 10, 0))
    assert sorted(chat for chat, _ in bot.sent) == [ADMIN, FRIEND]
    text = bot.sent[0][1]
    assert "BULLISH · TCS</b> <i>(provisional)</i>" in text and "at 10:00 on the live price" in text
    assert "INFY" not in text
    assert db.get(IntradaySignal, ("TCS", today)).active

    bot.sent.clear()
    run(bot, FakeLive({"TCS": "121", "INFY": "100"}), at(today, 10, 15))
    assert bot.sent == []  # still crossed: nothing new


def test_outside_session_and_first_15_minutes_are_skipped(db):
    today = history(db)
    setup_users(db)
    bot = FakeBot()
    live = FakeLive({"TCS": "120", "INFY": "100"})

    assert run(bot, live, at(today, 9, 15)) is None  # opening candle
    assert run(bot, live, at(today, 15, 30)) is None  # closing
    assert run(bot, FakeLive({"TCS": "120"}, closed={today}), at(today, 11, 0)) is None  # holiday
    assert bot.sent == [] and kinds(db) == []


def test_flip_flopping_is_capped_at_three_cross_alerts(db):
    today = history(db)
    setup_users(db)
    bot = FakeBot()
    for i, price in enumerate(["120", "100", "120", "100", "120", "100", "120"]):
        run(bot, FakeLive({"TCS": price, "INFY": "100"}), at(today, 10, 0) + timedelta(minutes=15 * i))

    C, U = AlertKind.CROSS, AlertKind.UNDONE
    assert kinds(db) == [("TCS", C, 1), ("TCS", U, 1), ("TCS", C, 2), ("TCS", U, 2), ("TCS", C, 3)]
    admin_texts = [text for chat, text in bot.sent if chat == ADMIN]
    assert len(admin_texts) == 5
    assert "no more intraday alerts for TCS until the close" in admin_texts[-1]
    row = db.get(IntradaySignal, ("TCS", today))
    assert (row.crosses, row.active) == (4, True)


def test_evening_confirms_a_cross_that_held(db):
    today = history(db)
    setup_users(db)
    bot = FakeBot()
    run(bot, FakeLive({"TCS": "120", "INFY": "100"}), at(today, 10, 0))
    bot.sent.clear()

    evening(bot, today, {"TCS": "119", "INFY": "100"})
    assert sorted(chat for chat, _ in bot.sent) == [ADMIN, FRIEND]
    assert "✅ <b>CONFIRMED · BULLISH · TCS</b>" in bot.sent[0][1]


def test_evening_cancels_a_cross_that_was_live_at_the_end(db):
    today = history(db)
    setup_users(db)
    bot = FakeBot()
    run(bot, FakeLive({"TCS": "120", "INFY": "100"}), at(today, 15, 15))
    bot.sent.clear()

    evening(bot, today, {"TCS": "100", "INFY": "100"})  # official close back to flat
    assert "❌ <b>NOT CONFIRMED · TCS</b>" in bot.sent[0][1]
    assert "Close ₹100.00" in bot.sent[0][1]


def test_an_undone_cross_gets_no_evening_message(db):
    today = history(db)
    setup_users(db)
    bot = FakeBot()
    run(bot, FakeLive({"TCS": "120", "INFY": "100"}), at(today, 10, 0))
    run(bot, FakeLive({"TCS": "100", "INFY": "100"}), at(today, 10, 15))
    bot.sent.clear()

    evening(bot, today, {"TCS": "100", "INFY": "100"})
    assert bot.sent == []


def test_cross_only_at_the_close_reads_like_a_normal_alert(db):
    today = history(db)
    setup_users(db)
    bot = FakeBot()
    run(bot, FakeLive({"TCS": "100", "INFY": "100"}), at(today, 15, 15))
    assert bot.sent == []

    evening(bot, today, {"TCS": "120", "INFY": "100"})
    text = bot.sent[0][1]
    assert "🟢 <b>BULLISH · TCS</b>" in text and "at the close" in text and "CONFIRMED" not in text


def test_split_today_scales_history_instead_of_firing(db):
    today = history(db)
    setup_users(db)
    bot = FakeBot()
    # 2:1 split this morning: Upstox's previous close is already halved, ours isn't until the evening.
    live = FakeLive({"TCS": "50", "INFY": "100"}, prev_close={"TCS": "50"})

    run(bot, live, at(today, 10, 0))
    assert bot.sent == []
    (p,) = [p for p in intraday.evaluate_live(db, live, today) if p.symbol == "TCS"]
    assert p.factor == Decimal("0.5") and p.direction is None


def test_missed_evening_run_is_caught_up_first(db):
    yesterday = history(db)  # loaded up to the day before `yesterday`
    today = yesterday + timedelta(days=1)
    setup_users(db)
    bot = FakeBot()
    nse = FakeSource({yesterday: {"TCS": "100", "INFY": "100"}})

    run(bot, FakeLive({"TCS": "120", "INFY": "100"}), at(today, 10, 0), nse)
    assert "BULLISH · TCS</b> <i>(provisional)</i>" in bot.sent[0][1]
    assert db.get(IntradaySignal, ("TCS", today)) is not None


def test_stale_history_pauses_intraday_and_tells_admin_once(db):
    yesterday = history(db)
    today = yesterday + timedelta(days=1)
    setup_users(db)
    bot = FakeBot()
    live = FakeLive({"TCS": "120", "INFY": "100"})

    assert run(bot, live, at(today, 10, 0)) is None  # NSE never published `yesterday`
    assert run(bot, live, at(today, 10, 15)) is None
    assert [chat for chat, _ in bot.sent] == [ADMIN]
    assert "Intraday check" in bot.sent[0][1]


def test_token_expiry_reminders_go_out_once_each(db):
    bot = FakeBot()
    expiry = datetime(2027, 10, 19, 3, 30, tzinfo=IST)

    def remind(days_before):
        return asyncio.run(intraday.remind_expiry(bot, expiry, expiry - timedelta(days=days_before), ADMIN))

    assert not remind(40)
    assert remind(25)
    assert not remind(24)
    assert remind(5)
    assert [chat for chat, _ in bot.sent] == [ADMIN, ADMIN]
    assert "expires on 19 Oct 2027" in bot.sent[0][1]
