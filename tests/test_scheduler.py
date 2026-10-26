import asyncio
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_daily import at
from tradio import bot, intraday
from tradio.data.upstox import MarketHours


def test_scheduler_rearms_even_if_tick_fails(monkeypatch):
    tick = AsyncMock(side_effect=RuntimeError("temporary failure"))
    schedule = AsyncMock()
    monkeypatch.setattr(bot, "_tick", tick)
    monkeypatch.setattr(bot, "_schedule_next_tick", schedule)
    app = object()
    with pytest.raises(RuntimeError, match="temporary failure"):
        asyncio.run(bot._scheduled_tick(SimpleNamespace(application=app)))
    schedule.assert_awaited_once_with(app)


def test_slow_timings_lookup_cannot_schedule_a_past_job(monkeypatch):
    day = date(2026, 10, 23)
    moments = iter([at(day, 9, 29) + timedelta(seconds=59), at(day, 9, 30) + timedelta(seconds=10)])
    class Clock:
        @staticmethod
        def now(tz):
            return next(moments)
    class Live:
        def market_hours(self, day):
            return MarketHours(at(day, 9, 15), at(day, 15, 30))
    scheduled = []
    app = SimpleNamespace(bot_data={"live": Live()}, job_queue=SimpleNamespace(
        run_once=lambda callback, when: scheduled.append((callback, when))))
    monkeypatch.setattr(bot, "datetime", Clock)
    asyncio.run(bot._schedule_next_tick(app))
    assert scheduled == [(bot._scheduled_tick, at(day, 9, 45) + intraday.PUBLISH_DELAY)]
