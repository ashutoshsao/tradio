"""Telegram message text (HTML). Pure functions."""

from datetime import date
from decimal import Decimal
from html import escape
from itertools import groupby
from urllib.parse import quote

from tradio.models import Direction, SmaState, User
from tradio.watchlist import AddResult, AlertItem, StockStatus

DISCLAIMER = "<i>Not investment advice.</i>"

HELP = f"""<b>Tradio</b> alerts you when a stock's daily SMA8 crosses above both SMA13 and SMA21 (bullish) or below both (bearish). Checked once a day after NSE publishes closing prices (~6–7 PM IST).

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
/status – last data loaded
/testalert – preview an alert using your watchlist's latest crosses"""

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
    head = f"<b>{escape(s.symbol)}</b>"
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
        f"⚠️ No NSE bhavcopy for {day(d)} by 10 PM IST. Probably a market holiday; "
        "if not, NSE is late and the next run will pick it up."
    )


def run_failed(d: date, exc: Exception) -> str:
    return f"⚠️ Daily run for {day(d)} failed: <code>{escape(repr(exc))[:500]}</code>\nIt retries every 15 minutes until 10 PM."


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
    return f"Access request from {who} (ID <code>{user_id}</code>).\nApprove with /allow {user_id}"


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
_HEADLINE = {
    Direction.BULLISH: ("🟢", "BULLISH", "SMA8 crossed above SMA13 &amp; SMA21"),
    Direction.BEARISH: ("🔴", "BEARISH", "SMA8 crossed below SMA13 &amp; SMA21"),
}


def chart_url(symbol: str) -> str:
    # TradingView writes "&" and "-" in NSE symbols as "_" (M&M -> M_M, BAJAJ-AUTO -> BAJAJ_AUTO).
    return "https://www.tradingview.com/chart/?symbol=" + quote(f"NSE:{symbol.replace('&', '_').replace('-', '_')}")


def alert_block(a: AlertItem) -> str:
    icon, label, what = _HEADLINE[a.direction]
    return (
        f"{icon} <b>{label} · {escape(a.symbol)}</b>\n"
        f"{escape(a.name)}\n"
        f"{what}\n"
        f"Close ₹{price(a.close)} · SMA8 {price(a.sma8)} · SMA13 {price(a.sma13)} · SMA21 {price(a.sma21)}\n"
        f'<a href="{chart_url(a.symbol)}">Open chart ↗</a>'
    )


def digest(alerts: list[AlertItem], test: bool = False) -> list[str]:
    """One user's alerts as Telegram messages (split to fit the length limit), grouped by trading day."""
    header = "🧪 <b>Test alert</b> – this is how alerts will look (using each stock's most recent cross)\n\n" if test else ""
    blocks = []
    ordered = sorted(alerts, key=lambda a: (a.trade_date, a.direction != Direction.BULLISH, a.symbol))
    for trade_date, items in groupby(ordered, key=lambda a: a.trade_date):
        blocks.append(f"📈 <b>Tradio · {day(trade_date)}</b>")
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
