"""Telegram bot: commands, search buttons and inline autocomplete. Logic lives in tradio.watchlist.

See docs/v1-spec.md §7.
"""

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, time, timedelta

from sqlalchemy import func, select
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

from tradio import daily, intraday, messages
from tradio import watchlist as wl
from tradio.config import IST, get_settings
from tradio.data.nse import NseArchiveSource
from tradio.data.upstox import UpstoxSource, token_expiry
from tradio.db import session
from tradio.engine import evaluate
from tradio.models import AlertKind, User, WatchlistEntry

log = logging.getLogger(__name__)

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


async def db(fn, *args):
    """Run a blocking database call off the event loop with its own session."""

    def call():
        with session() as s:
            return fn(s, *args)

    return await asyncio.to_thread(call)


async def reply(update: Update, text: str, buttons: InlineKeyboardMarkup | None = None) -> None:
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=buttons
    )


def _add_buttons(symbols_and_labels: list[tuple[str, str]]) -> InlineKeyboardMarkup | None:
    if not symbols_and_labels:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"➕ {label}", callback_data=f"add:{symbol}")] for symbol, label in symbols_and_labels]
    )


def _remove_buttons(symbols: list[str]) -> InlineKeyboardMarkup | None:
    if not symbols:
        return None
    rows = [symbols[i : i + 3] for i in range(0, len(symbols), 3)]
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"❌ {s}", callback_data=f"rm:{s}") for s in row] for row in rows])


def _current_user(s, user_id: int) -> User | None:
    user = s.get(User, user_id)
    return user if user and user.is_allowed else None


def allowed(handler: Handler) -> Handler:
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await db(_current_user, update.effective_user.id) is None:
            if update.inline_query:
                await update.inline_query.answer([], cache_time=10, is_personal=True)
            elif update.callback_query:
                await update.callback_query.answer("Not allowed")
            else:
                await reply(update, messages.not_allowed(update.effective_user.id))
            return
        await handler(update, context)

    return wrapper


def admin_only(handler: Handler) -> Handler:
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = await db(_current_user, update.effective_user.id)
        if user is None or not user.is_admin:
            return  # don't reveal admin commands
        await handler(update, context)

    return wrapper


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    settings = get_settings()
    user = await db(wl.register, tg.id, update.effective_chat.id, tg.username, settings.admin_telegram_id)
    if user.is_allowed:
        await reply(update, messages.welcome(user))
        return
    await reply(update, messages.not_allowed(tg.id))
    if settings.admin_telegram_id:
        await context.bot.send_message(
            settings.admin_telegram_id,
            messages.access_request(tg.id, tg.username, tg.full_name),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Allow", callback_data=f"allow:{tg.id}")]]),
        )


@allowed
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await db(_current_user, update.effective_user.id)
    await reply(update, messages.welcome(user))


@allowed
async def add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await reply(update, messages.add_result(wl.AddResult([], [], {}, []), None))
        return
    await _add_and_reply(update, context.args)


async def _add_and_reply(update: Update, symbols: list[str]) -> None:
    result = await db(wl.add_symbols, update.effective_user.id, symbols)
    today = await db(wl.latest_trading_day)
    suggestions = [(s, s) for options in result.unknown.values() for s in options]
    await reply(update, messages.add_result(result, today), _add_buttons(suggestions))


@allowed
async def add_from_inline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A message sent by picking an inline-mode result: "/add SYMBOL"."""
    await _add_and_reply(update, update.effective_message.text.split()[1:])


@allowed
async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.effective_message.text.strip()[:50]
    matches = await db(wl.search, query)
    await reply(update, messages.search_results(query, matches), _add_buttons([(m.symbol, f"{m.symbol} · {m.name}") for m in matches]))


@allowed
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, _, symbol = query.data.partition(":")
    if action == "add":
        result = await db(wl.add_symbols, update.effective_user.id, [symbol])
        await query.answer(f"Watching {symbol}" if result.added else f"Already watching {symbol}")
        if result.added:
            today = await db(wl.latest_trading_day)
            await reply(update, messages.add_result(result, today))
    elif action == "rm":
        await db(wl.remove_symbols, update.effective_user.id, [symbol])
        await query.answer(f"Removed {symbol}")
        statuses = await db(wl.watchlist, update.effective_user.id)
        today = await db(wl.latest_trading_day)
        await query.edit_message_text(
            messages.watchlist(statuses, today),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=_remove_buttons([st.symbol for st in statuses]),
        )


@allowed
async def inline_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.inline_query.query.strip()
    matches = await db(wl.search, text, 10) if text else []
    results = [
        InlineQueryResultArticle(
            id=m.symbol,
            title=m.symbol,
            description=f"{m.name} · tap to watch",
            input_message_content=InputTextMessageContent(f"/add {m.symbol}"),
        )
        for m in matches
    ]
    await update.inline_query.answer(results, cache_time=60, is_personal=True)


@allowed
async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    removed, missing = await db(wl.remove_symbols, update.effective_user.id, context.args or [])
    await reply(update, messages.remove_result(removed, missing))


@allowed
async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    statuses = await db(wl.watchlist, update.effective_user.id)
    today = await db(wl.latest_trading_day)
    await reply(update, messages.watchlist(statuses, today), _remove_buttons([s.symbol for s in statuses]))


def _grant(s, user_id: int, allow: bool) -> tuple[User | None, bool]:
    """(user, changed). None if there's no such user or it's the admin."""
    user = s.get(User, user_id)
    was = user.is_allowed if user else None
    user = wl.set_allowed(s, user_id, allow)
    return user, user is not None and was != allow


async def _welcome(context: ContextTypes.DEFAULT_TYPE, user: User) -> None:
    await context.bot.send_message(user.chat_id, "You're in! " + messages.HELP, parse_mode=ParseMode.HTML)


async def _set_access(update: Update, context: ContextTypes.DEFAULT_TYPE, allow: bool) -> None:
    if not context.args or not context.args[0].isdigit():
        await reply(update, f"Usage: /{'allow' if allow else 'revoke'} TELEGRAM_ID")
        return
    user_id = int(context.args[0])
    user, changed = await db(_grant, user_id, allow)
    if user is None:
        await reply(update, "No such user (they need to send /start first), or it's the admin.")
        return
    await reply(update, f"{'Allowed' if allow else 'Revoked'} <code>{user_id}</code>.")
    if allow and changed:
        await _welcome(context, user)


@admin_only
async def allow_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The ✅ Allow button on an access request."""
    query = update.callback_query
    user, changed = await db(_grant, int(query.data.partition(":")[2]), True)
    if user is None:
        await query.answer("No such user any more")
        return
    await query.answer("Allowed" if changed else "Already allowed")
    await query.edit_message_text(query.message.text_html + "\n\n✅ Allowed", parse_mode=ParseMode.HTML)
    if changed:
        await _welcome(context, user)


@admin_only
async def allow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_access(update, context, True)


@admin_only
async def revoke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_access(update, context, False)


@admin_only
async def delete_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].isdigit():
        await reply(update, "Usage: /deleteuser TELEGRAM_ID")
        return
    user_id = int(context.args[0])
    if await db(wl.delete_user, user_id) is None:
        await reply(update, "No such user, or it's the admin.")
        return
    await reply(update, f"Deleted <code>{user_id}</code> and their watchlist. If they /start again they'll need approval.")


@admin_only
async def users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, messages.users(await db(wl.list_users)))


def _test_alerts(s, user_id: int) -> list[list[wl.AlertItem]]:
    """Sample digests built from the watchlist's latest crosses: one during market hours, one after the close."""
    symbols = [st.symbol for st in wl.watchlist(s, user_id)] or ["RELIANCE", "HDFCBANK"]
    evaluate(s, symbols)  # make sure sample stocks have their history computed
    s.commit()
    items = wl.latest_alerts(s, symbols)
    if not items:
        return []
    # Pretend every sample happened on the latest of those days, so they read as one day's alerts.
    sample_day = max(a.trade_date for a in items)
    first, *rest = [replace(a, trade_date=sample_day) for a in items]
    at = lambda hh, mm: datetime.combine(sample_day, time(hh, mm), IST)  # noqa: E731
    live = [replace(first, kind=AlertKind.CROSS, seq=1, at=at(10, 45))]
    close = [replace(first, after_intraday=True), *rest]
    if rest:
        # A cross that later fails: SMA8 back between the other two, as it would really be.
        between = replace(rest[0], sma8=(rest[0].sma13 + rest[0].sma21) / 2)
        live.append(replace(rest[0], kind=AlertKind.CROSS, seq=1, at=at(10, 45)))
        live.append(replace(between, kind=AlertKind.UNDONE, seq=1, at=at(13, 0)))
        close[1] = replace(between, kind=AlertKind.NOT_CONFIRMED)
    return [live, close]


@admin_only
async def test_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    samples = await db(_test_alerts, update.effective_user.id)
    if not samples:
        await reply(update, "No crosses in your stocks' history to build a sample from.")
        return
    for title, alerts in zip(["during market hours", "after the close"], samples):
        for chunk in messages.digest(alerts, test_title=title):
            await reply(update, chunk)


def _status(s) -> str:
    latest = wl.latest_trading_day(s)
    watched = s.scalar(select(func.count(func.distinct(WatchlistEntry.symbol))))
    token = get_settings().upstox_analytics_token
    expiry = token_expiry(token.get_secret_value()) if token else None
    upstox = f"expires {messages.day(expiry.date())}" if expiry else "not set (no intraday alerts)"
    return (
        f"Latest trading day loaded: {messages.day(latest) if latest else 'none'}\n"
        f"Stocks watched: {watched}\nUpstox token: {upstox}"
    )


@admin_only
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, await db(_status))


COMMANDS = [
    ("add", "Watch stocks, e.g. /add RELIANCE TCS"),
    ("remove", "Stop watching stocks"),
    ("list", "Your stocks and where each stands"),
    ("help", "How Tradio works"),
]


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
    token = get_settings().upstox_analytics_token
    if token:
        app.bot_data["live"] = UpstoxSource(token.get_secret_value())
        app.bot_data["expiry"] = token_expiry(token.get_secret_value())
    else:
        log.warning("UPSTOX_ANALYTICS_TOKEN not set: intraday alerts are off")
    # Catch up once at startup (e.g. after the laptop was off), then every 15 minutes.
    # Each tick does whatever is due: intraday checks in market hours, the evening run 18:00-22:00.
    app.job_queue.run_once(_tick, when=5, data={"force_evening": True})
    now = datetime.now(IST)
    next_quarter = (now + timedelta(minutes=15 - now.minute % 15)).replace(second=0, microsecond=0)
    app.job_queue.run_repeating(_tick, interval=timedelta(minutes=15), first=next_quarter)


async def _tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(IST)
    admin_id = get_settings().admin_telegram_id
    nse = NseArchiveSource()
    if live := context.bot_data.get("live"):
        try:
            await intraday.remind_expiry(context.bot, context.bot_data.get("expiry"), now, admin_id)
        except Exception:
            log.exception("token expiry reminder failed")
        await intraday.tick(context.bot, live, nse, now, admin_id)  # skips itself outside market hours
    if (context.job.data or {}).get("force_evening") or daily.in_window(now):
        try:
            await daily.tick(context.bot, nse, now, admin_id)
        except Exception:
            pass  # logged and reported to the admin inside tick; the next tick retries


def build_application() -> Application:
    token = get_settings().telegram_bot_token
    if token is None:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set (see .env.example)")
    app = Application.builder().token(token.get_secret_value()).post_init(_post_init).build()
    # Picking an inline result sends "/add SYMBOL" via the bot; catch it before the command handlers.
    app.add_handler(MessageHandler(filters.VIA_BOT & filters.Regex(r"^/add\s"), add_from_inline))
    for name, handler in [
        ("start", start),
        ("help", help_command),
        ("add", add),
        ("remove", remove),
        ("list", list_command),
        ("allow", allow),
        ("revoke", revoke),
        ("deleteuser", delete_user),
        ("users", users),
        ("status", status),
        ("testalert", test_alert),
    ]:
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(button, pattern=r"^(add|rm):"))
    app.add_handler(CallbackQueryHandler(allow_button, pattern=r"^allow:\d+$"))
    app.add_handler(InlineQueryHandler(inline_search))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.VIA_BOT, search))
    return app


def run() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)  # every poll is logged otherwise
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)
