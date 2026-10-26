"""Telegram message text (HTML). Pure functions."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from html import escape
from itertools import groupby
from urllib.parse import quote

from tradio.config import IST
from tradio.models import AlertKind, Direction, SmaState, User
from tradio.signals import MAX_CROSSES, classify
from tradio.watchlist import AddResult, AlertItem, StockStatus

DISCLAIMER = "<i>Not investment advice.</i>"

HELP = f"""<b>Tradio</b> alerts you when SMA8 crosses above both SMA13 and SMA21 (bullish) or below both (bearish).

During market hours it checks completed <b>15-minute candles</b>, five seconds after each candle closes (normally 09:30:05–15:30:05 IST). SMA8 averages 8 candle closes; SMA13 and SMA21 average 13 and 21. These signals are confirmed at candle close; a later reversal is a new signal. Directions alternate, with no daily alert cap.

Separate <b>daily</b> crossover alerts arrive after NSE publishes the official daily close (~6–7 PM IST). /list shows daily states. Daily and 15-minute signals can differ.

Just type part of a company name (e.g. <code>hdfc</code> or <code>tata motors</code>) and tap a result to watch it.
Or type <code>@TradioxBot hdf…</code> for live suggestions as you type.

/add SYMBOL … – watch stocks by symbol, e.g. <code>/add RELIANCE TCS</code>
/remove SYMBOL … – stop watching
/list – your stocks and where each stands now
/help – this message

{DISCLAIMER}"""

ADMIN_HELP = """<b>Admin</b>
/allow ID – let a user in
/revoke ID – block access (keeps their watchlist; /allow restores it)
/deleteuser ID – remove the user and their watchlist
/users – everyone who has started the bot
/status – last data loaded, Upstox token expiry
/testalert – preview intraday and evening alerts using your watchlist"""

_STATE = {
    SmaState.ABOVE: "🟢 SMA8 above both",
    SmaState.BELOW: "🔴 SMA8 below both",
    SmaState.MIXED: "⚪ between (no signal)",
}
_DIRECTION = {Direction.BULLISH: "🟢 bullish", Direction.BEARISH: "🔴 bearish"}


def day(d: date) -> str:
    return f"{d.day} {d:%b %Y}"


def price(value: Decimal) -> str:
    return f"{value:,.2f}"


def status_line(s: StockStatus, today: date | None = None) -> str:
    head = f"<b>{escape(s.symbol)}</b> · daily"
    if s.last_seen and today and s.last_seen < today:
        return f"{head} – no NSE data since {day(s.last_seen)}"
    if s.state is None:
        return f"{head} – not enough history yet; alerts start in {s.days_until_ready} trading days"
    line = (
        f"{head} – {_STATE[s.state]} (as of {day(s.as_of)})\n"
        f"    Close ₹{price(s.close)} · SMA8 {price(s.sma8)} · SMA13 {price(s.sma13)} · SMA21 {price(s.sma21)}"
    )
    if s.last_event:
        line += f"\n    Last cross: {_DIRECTION[s.last_event[1]]} on {day(s.last_event[0])}"
    return line


def no_data(d: date) -> str:
    return (
        f"⚠️ No NSE bhavcopy for {day(d)} by 10 PM IST, though NSE was open that day. "
        "NSE is probably late; it's loaded before tomorrow's first intraday check."
    )


def run_failed(d: date, exc: Exception) -> str:
    return f"⚠️ Daily run for {day(d)} failed: <code>{escape(repr(exc))[:500]}</code>\nIt retries every 15 minutes until 10 PM."


def intraday_failed(d: date, exc: Exception) -> str:
    return (
        f"⚠️ Intraday check on {day(d)} failed: <code>{escape(repr(exc))[:500]}</code>\n"
        "It retries every 15 minutes; this message is sent once a day. The evening run still sends confirmed crosses."
    )


def token_expiring(expiry: datetime, days_left: int) -> str:
    return (
        f"⏳ The Upstox Analytics Token expires on {day(expiry.date())} ({max(days_left, 0)} days). "
        "Generate a new one at account.upstox.com/developer/apps (Analytics tab) and put it in "
        "<code>UPSTOX_ANALYTICS_TOKEN</code>, then restart the bot. Until then intraday alerts stop; evening alerts carry on."
    )


def search_results(query: str, matches) -> str:
    if not matches:
        return f"No NSE stock matches <b>{escape(query)}</b>. Try another part of the name or the symbol."
    return f"🔍 <b>{escape(query)}</b> – tap to watch:"


def welcome(user: User) -> str:
    return HELP + ("\n\n" + ADMIN_HELP if user.is_admin else "")


def not_allowed(user_id: int) -> str:
    return f"You're not on the list yet. Send this ID to the admin: <code>{user_id}</code>"


def access_request(user_id: int, username: str | None, name: str) -> str:
    who = f"@{escape(username)}" if username else escape(name)
    return f"Access request from {who} (ID <code>{user_id}</code>).\nTap Allow, or send /allow {user_id}"


def add_result(r: AddResult, today: date | None) -> str:
    parts = []
    if r.added:
        parts.append("Added. You'll get an alert on the next cross:\n\n" + "\n\n".join(status_line(s, today) for s in r.added))
    if r.already:
        parts.append("Already watching: " + ", ".join(escape(s) for s in r.already))
    for symbol, suggestions in r.unknown.items():
        hint = f" Did you mean {', '.join(escape(x) for x in suggestions)}?" if suggestions else ""
        parts.append(f"<b>{escape(symbol)}</b> isn't an NSE stock that traded on the last trading day.{hint}")
    if r.full:
        parts.append(f"Watchlist is full, not added: {', '.join(escape(s) for s in r.full)}")
    return "\n\n".join(parts) or "Usage: /add SYMBOL …  e.g. <code>/add RELIANCE TCS</code>"


def remove_result(removed: list[str], missing: list[str]) -> str:
    parts = []
    if removed:
        parts.append("Removed: " + ", ".join(escape(s) for s in removed))
    if missing:
        parts.append("Not on your list: " + ", ".join(escape(s) for s in missing))
    return "\n".join(parts) or "Usage: /remove SYMBOL …"


def watchlist(statuses: list[StockStatus], today: date | None) -> str:
    if not statuses:
        return "You're not watching anything yet. Try <code>/add RELIANCE</code>"
    return f"<b>Your stocks ({len(statuses)})</b>\n\n" + "\n\n".join(status_line(s, today) for s in statuses)


def users(rows: list[tuple[User, int]]) -> str:
    lines = []
    for user, watching in rows:
        name = f"@{escape(user.username)}" if user.username else "(no username)"
        role = "admin" if user.is_admin else "allowed" if user.is_allowed else "waiting"
        lines.append(f"<code>{user.telegram_user_id}</code> {name} – {role}, {watching} stocks")
    return "<b>Users</b>\n" + "\n".join(lines) if lines else "No users yet."


# --- Alerts -------------------------------------------------------------------

TELEGRAM_LIMIT = 4096
_ICON = {Direction.BULLISH: "🟢", Direction.BEARISH: "🔴"}
_BACK_TO = {
    SmaState.MIXED: "SMA8 back between 13 &amp; 21",
    SmaState.ABOVE: "SMA8 back above 13 &amp; 21",
    SmaState.BELOW: "SMA8 back below 13 &amp; 21",
}


def chart_url(symbol: str) -> str:
    # TradingView writes "&" and "-" in NSE symbols as "_" (M&M -> M_M, BAJAJ-AUTO -> BAJAJ_AUTO).
    return "https://www.tradingview.com/chart/?symbol=" + quote(f"NSE:{symbol.replace('&', '_').replace('-', '_')}")


def clock(dt: datetime) -> str:
    """A time each reader's Telegram shows in their own time zone and 12/24-hour setting.

    Apps without date-time entities show the fallback text, in IST. Kept outside <b> so
    no entity nesting is needed.
    """
    return f'<tg-time unix="{int(dt.timestamp())}" format="t">{dt.astimezone(IST):%H:%M} IST</tg-time>'


def _smas(a: AlertItem) -> str:
    return f"SMA 8/13/21: {price(a.sma8)} · {price(a.sma13)} · {price(a.sma21)}"


def _chart(a: AlertItem) -> str:
    interval = "15" if a.kind == AlertKind.CANDLE else "D"
    return f'<a href="{chart_url(a.symbol)}&amp;interval={interval}">Chart ↗</a>'


def alert_block(a: AlertItem) -> str:
    """One stock's alert: a headline that carries the meaning, then price, chart and the three SMAs."""
    symbol, cross = escape(a.symbol), f"{a.direction.value} cross"
    if a.kind == AlertKind.CANDLE:
        end = a.candle_start + timedelta(minutes=15)
        return "\n".join([
            f"{_ICON[a.direction]} <b>{symbol} · {cross} · 15m</b>",
            escape(a.name),
            f"Candle {a.candle_start:%H:%M}–{end:%H:%M} IST · closed {clock(end)}",
            f"Close ₹{price(a.price)} · {_chart(a)}", _smas(a),
        ])
    elif a.kind == AlertKind.CROSS:
        lines = [
            f"{_ICON[a.direction]} <b>{symbol} · {cross}</b>",
            escape(a.name),
            f"₹{price(a.price)} at {clock(a.at)} · {_chart(a)}",
            _smas(a),
        ]
        if a.seq >= MAX_CROSSES:
            lines.append(f"<i>Cross #{a.seq} today: no more live alerts for {symbol} until the close.</i>")
    elif a.kind == AlertKind.UNDONE:
        lines = [
            f"↩️ <b>{symbol} · cross undone</b> at {clock(a.at)}",
            f"₹{price(a.price)} · {_BACK_TO[classify(a.sma8, a.sma13, a.sma21)]}",
        ]
    elif a.kind == AlertKind.NOT_CONFIRMED:
        lines = [f"❌ <b>{symbol} · {cross} not confirmed</b>", f"Close ₹{price(a.price)} · didn't hold", _smas(a)]
    elif a.after_intraday:
        lines = [f"✅ <b>{symbol} · {cross} confirmed</b>", f"Close ₹{price(a.price)} · {_chart(a)}", _smas(a)]
    else:  # crossed only at the close
        lines = [
            f"{_ICON[a.direction]} <b>{symbol} · {cross} at the close</b>",
            escape(a.name),
            f"Close ₹{price(a.price)} · {_chart(a)}",
            _smas(a),
        ]
    lines[0] += " · daily"
    return "\n".join(lines)


_KIND_ORDER = {AlertKind.CONFIRMED: 0, AlertKind.NOT_CONFIRMED: 1, AlertKind.CROSS: 2, AlertKind.UNDONE: 3, AlertKind.CANDLE: 4}
_LIVE = (AlertKind.CROSS, AlertKind.UNDONE)


def _day_header(trade_date: date, items: list[AlertItem]) -> str:
    live = [a for a in items if a.kind in _LIVE]
    header = f"📈 <b>Tradio · {trade_date.day} {trade_date:%b}</b>"
    if live and len(live) == len(items):
        header += f" · {clock(max(a.at for a in live))} · live"
    elif all(a.kind == AlertKind.CANDLE for a in items):
        header += " · 15m · completed candles"
    elif not live and not any(a.kind == AlertKind.CANDLE for a in items):
        header += " · close"
    if live:
        header += "\n⏳ <i>Provisional: final verdict after the close</i>"
    return header


def digest(alerts: list[AlertItem], test_title: str | None = None) -> list[str]:
    """One user's alerts as Telegram messages (split to fit the length limit), grouped by trading day."""
    header = f"🧪 <b>Test · {test_title}</b>\n" if test_title else ""
    blocks = []
    ordered = sorted(alerts, key=lambda a: (a.trade_date, _KIND_ORDER[a.kind], a.seq if a.kind == AlertKind.CANDLE else 0, a.direction != Direction.BULLISH, a.symbol))
    for trade_date, group in groupby(ordered, key=lambda a: a.trade_date):
        items = list(group)
        blocks.append(_day_header(trade_date, items))
        blocks.extend(alert_block(a) for a in items)
    blocks.append(DISCLAIMER)

    chunks, current = [], header
    for block in blocks:
        if current and len(current) + len(block) + 2 > TELEGRAM_LIMIT:
            chunks.append(current.rstrip())
            current = ""
        current += block + "\n\n"
    chunks.append(current.rstrip())
    return chunks
