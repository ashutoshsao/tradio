from datetime import date, timedelta

from sqlalchemy import func, select

from bhavcopy_fixture import csv_text, row
from tradio import messages
from tradio import watchlist as wl
from tradio.data.nse import parse_bhavcopy_csv
from tradio.ingest import store_day
from tradio.models import SignalEvent, SmaState, TradingDay

START = date(2026, 1, 1)
ADMIN, FRIEND = 111, 222


def load(db, series: dict[str, list[float]], days: int):
    for i in range(days):
        day = START + timedelta(days=i)
        rows = [row(day, sym, str(closes[i])) for sym, closes in series.items() if i < len(closes) and closes[i]]
        store_day(db, parse_bhavcopy_csv(csv_text(*rows)))
        db.add(TradingDay(trade_date=day, actions_loaded=True))
    db.commit()


def test_admin_is_allowed_others_wait(db):
    assert wl.register(db, ADMIN, ADMIN, "me", admin_id=ADMIN).is_allowed
    friend = wl.register(db, FRIEND, FRIEND, "pal", admin_id=ADMIN)
    assert not friend.is_allowed

    assert wl.set_allowed(db, FRIEND, True).is_allowed
    assert wl.set_allowed(db, ADMIN, False) is None  # can't lock out the admin
    assert wl.set_allowed(db, 999, True) is None


def test_delete_user_removes_watchlist_but_not_admin(db):
    load(db, {"A": [1] * 25}, days=25)
    wl.register(db, ADMIN, ADMIN, None, admin_id=ADMIN)
    wl.register(db, FRIEND, FRIEND, None, admin_id=ADMIN)
    wl.set_allowed(db, FRIEND, True)
    wl.add_symbols(db, FRIEND, ["A"])

    assert wl.delete_user(db, FRIEND) is not None
    assert wl.watchlist(db, FRIEND) == []
    assert [u.telegram_user_id for u, _ in wl.list_users(db)] == [ADMIN]
    assert wl.delete_user(db, ADMIN) is None


def test_add_shows_current_state_and_does_not_queue_past_signals(db):
    load(db, {"TCS": [100] * 25 + [110] * 5}, days=30)
    wl.register(db, ADMIN, ADMIN, None, admin_id=ADMIN)

    result = wl.add_symbols(db, ADMIN, ["tcs", "TCS.NS"])
    (status,) = result.added
    assert status.symbol == "TCS"
    assert status.state == SmaState.ABOVE
    assert status.as_of == START + timedelta(days=29)
    assert status.last_event[0] == START + timedelta(days=25)
    # History is evaluated (events exist) but nothing is queued for delivery here.
    assert db.scalar(select(func.count()).select_from(SignalEvent)) == 1

    again = wl.add_symbols(db, ADMIN, ["TCS"])
    assert again.added == [] and again.already == ["TCS"]


def test_add_rejects_unknown_and_stale_symbols_with_suggestions(db):
    load(db, {"RELIANCE": [100] * 30, "OLDCO": [50] * 10}, days=30)  # OLDCO stopped trading
    wl.register(db, ADMIN, ADMIN, None, admin_id=ADMIN)

    result = wl.add_symbols(db, ADMIN, ["RELIANC", "OLDCO"])
    assert result.added == []
    assert result.unknown == {"RELIANC": ["RELIANCE"], "OLDCO": []}


def test_new_listing_reports_days_until_alerts(db):
    load(db, {"NEWCO": [None] * 20 + [10] * 10}, days=30)
    wl.register(db, ADMIN, ADMIN, None, admin_id=ADMIN)

    (status,) = wl.add_symbols(db, ADMIN, ["NEWCO"]).added
    assert status.state is None
    assert status.days_until_ready == 12  # needs 22 continuous closes, has 10


def test_watchlist_cap(db, monkeypatch):
    load(db, {"A": [1] * 25, "B": [1] * 25}, days=25)
    wl.register(db, ADMIN, ADMIN, None, admin_id=ADMIN)
    monkeypatch.setattr(wl, "MAX_WATCHLIST", 1)

    result = wl.add_symbols(db, ADMIN, ["A", "B"])
    assert [s.symbol for s in result.added] == ["A"]
    assert result.full == ["B"]


def test_remove_and_list(db):
    load(db, {"A": [1] * 25, "B": [1] * 25}, days=25)
    wl.register(db, ADMIN, ADMIN, None, admin_id=ADMIN)
    wl.add_symbols(db, ADMIN, ["A", "B"])

    assert wl.remove_symbols(db, ADMIN, ["a", "Z"]) == (["A"], ["Z"])
    assert [s.symbol for s in wl.watchlist(db, ADMIN)] == ["B"]


def test_messages_render(db):
    load(db, {"M&M": [100] * 25 + [110] * 5}, days=30)
    wl.register(db, ADMIN, ADMIN, None, admin_id=ADMIN)
    result = wl.add_symbols(db, ADMIN, ["M&M", "NOPE"])

    text = messages.add_result(result, wl.latest_trading_day(db))
    assert "<b>M&amp;M</b> – 🟢 SMA8 above both (as of 30 Jan 2026)" in text  # HTML-escaped
    assert "Last cross: 🟢 bullish on 26 Jan 2026" in text
    assert "<b>NOPE</b> isn't an NSE stock" in text
    assert messages.watchlist([], None).startswith("You're not watching anything")
