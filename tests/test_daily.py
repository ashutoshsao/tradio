import asyncio
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from telegram.error import Forbidden

from bhavcopy_fixture import csv_text, row
from tradio import daily
from tradio import watchlist as wl
from tradio.data.nse import parse_bhavcopy_csv
from tradio.ingest import store_day
from tradio.models import Delivery, DeliveryStatus, Run, RunStatus, TradingDay

ADMIN, FRIEND, STRANGER = 111, 222, 333
DAY0 = date(2026, 9, 1)  # history starts here, one trading day per calendar day for simplicity


class FakeSource:
    def __init__(self, closes_by_day: dict[date, dict[str, str]]):
        self.closes_by_day = closes_by_day

    def fetch_day(self, day):
        if day not in self.closes_by_day:
            return None
        return parse_bhavcopy_csv(csv_text(*(row(day, s, c) for s, c in self.closes_by_day[day].items())))

    def fetch_actions(self, day):
        return [] if day in self.closes_by_day else None

    def fetch_actions_between(self, start, end):
        return []


class FakeBot:
    def __init__(self, blocked=()):
        self.sent: list[tuple[int, str]] = []
        self.blocked = set(blocked)

    async def send_message(self, chat_id, text, **kwargs):
        if chat_id in self.blocked:
            raise Forbidden("bot was blocked by the user")
        self.sent.append((chat_id, text))


def history(db, days=30):
    """30 flat days of TCS and INFY already loaded."""
    for i in range(days):
        day = DAY0 + timedelta(days=i)
        store_day(db, parse_bhavcopy_csv(csv_text(row(day, "TCS", "100"), row(day, "INFY", "100"))))
        db.add(TradingDay(trade_date=day, actions_loaded=True))
    db.commit()
    return DAY0 + timedelta(days=days)  # the next trading day


def at(day, hh=19, mm=0):
    return datetime.combine(day, time(hh, mm), daily.IST)


def setup_users(db):
    wl.register(db, ADMIN, ADMIN, None, admin_id=ADMIN)
    wl.register(db, FRIEND, FRIEND, None, admin_id=ADMIN)
    wl.set_allowed(db, FRIEND, True)
    wl.register(db, STRANGER, STRANGER, None, admin_id=ADMIN)  # never allowed
    wl.add_symbols(db, ADMIN, ["TCS", "INFY"])
    wl.add_symbols(db, FRIEND, ["TCS"])


def test_new_cross_is_sent_once_to_each_watcher(db):
    today = history(db)
    setup_users(db)
    source = FakeSource({today: {"TCS": "120", "INFY": "100"}})  # TCS jumps: SMA8 above both
    bot = FakeBot()

    result = asyncio.run(daily.tick(bot, source, at(today), ADMIN))
    assert result.new_days == [today]
    assert sorted(chat for chat, _ in bot.sent) == [ADMIN, FRIEND]
    assert all("TCS · bullish cross at the close" in text and "INFY" not in text for _, text in bot.sent)
    assert db.get(Run, today).status == RunStatus.DONE

    bot.sent.clear()
    asyncio.run(daily.tick(bot, source, at(today, 19, 15), ADMIN))
    assert bot.sent == []  # idempotent


def test_before_publication_nothing_happens_then_admin_told_at_10pm(db):
    today = history(db)
    setup_users(db)
    source = FakeSource({})  # NSE hasn't published
    bot = FakeBot()
    weekday = today if today.weekday() < 5 else today + timedelta(days=7 - today.weekday())

    asyncio.run(daily.tick(bot, source, at(weekday, 18, 0), ADMIN))
    assert bot.sent == []
    asyncio.run(daily.tick(bot, source, at(weekday, 22, 0), ADMIN))
    assert [chat for chat, _ in bot.sent] == [ADMIN]
    assert "No NSE bhavcopy" in bot.sent[0][1]
    assert db.get(Run, weekday).status == RunStatus.NO_DATA


def test_catch_up_sends_recent_days_only(db):
    today = history(db, days=40)
    setup_users(db)
    # Laptop was off for 7 trading days; TCS crossed on the first of them, INFY on the last.
    missed = [today + timedelta(days=i) for i in range(7)]
    closes = {d: {"TCS": "120", "INFY": "100"} for d in missed}
    closes[missed[-1]]["INFY"] = "130"
    bot = FakeBot()

    asyncio.run(daily.tick(bot, FakeSource(closes), at(missed[-1]), ADMIN))
    admin_text = next(text for chat, text in bot.sent if chat == ADMIN)
    assert "INFY" in admin_text
    assert "TCS" not in admin_text  # 7 trading days old: too stale to send


def test_blocked_user_is_skipped_and_others_still_get_alerts(db):
    today = history(db)
    setup_users(db)
    bot = FakeBot(blocked={FRIEND})

    result = asyncio.run(daily.tick(bot, FakeSource({today: {"TCS": "120", "INFY": "100"}}), at(today), ADMIN))
    assert [chat for chat, _ in bot.sent] == [ADMIN]
    assert (result.sent_users, result.failed_users) == (1, 1)
    statuses = {d.user_id: d.status for d in db.scalars(select(Delivery))}
    assert statuses == {ADMIN: DeliveryStatus.SENT, FRIEND: DeliveryStatus.SKIPPED}


def test_revoked_user_gets_nothing(db):
    today = history(db)
    setup_users(db)
    wl.set_allowed(db, FRIEND, False)
    bot = FakeBot()

    asyncio.run(daily.tick(bot, FakeSource({today: {"TCS": "120", "INFY": "100"}}), at(today), ADMIN))
    assert [chat for chat, _ in bot.sent] == [ADMIN]
