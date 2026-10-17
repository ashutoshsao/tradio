"""Telegram message text (HTML). Pure functions."""

from datetime import date
from decimal import Decimal
from html import escape

from tradio.models import Direction, SmaState, User
from tradio.watchlist import AddResult, StockStatus

DISCLAIMER = "<i>Not investment advice.</i>"

HELP = f"""<b>Tradio</b> alerts you when a stock's daily SMA8 crosses above both SMA13 and SMA21 (bullish) or below both (bearish). Checked once a day after NSE publishes closing prices (~6–7 PM IST).

/add SYMBOL … – watch stocks, e.g. <code>/add RELIANCE TCS</code>
/remove SYMBOL … – stop watching
/list – your stocks and where each stands now
/help – this message

{DISCLAIMER}"""

ADMIN_HELP = """<b>Admin</b>
/allow ID – let a user in
/revoke ID – block access (keeps their watchlist; /allow restores it)
/deleteuser ID – remove the user and their watchlist
/users – everyone who has started the bot
/status – last data loaded"""

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
