"""Telegram bot: commands only. Logic lives in tradio.watchlist. See docs/v1-spec.md §7."""

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import func, select
from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from tradio import messages
from tradio import watchlist as wl
from tradio.config import get_settings
from tradio.db import session
from tradio.models import User, WatchlistEntry

log = logging.getLogger(__name__)

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


async def db(fn, *args):
    """Run a blocking database call off the event loop with its own session."""

    def call():
        with session() as s:
            return fn(s, *args)

    return await asyncio.to_thread(call)


async def reply(update: Update, text: str) -> None:
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


def _current_user(s, user_id: int) -> User | None:
    user = s.get(User, user_id)
    return user if user and user.is_allowed else None


def allowed(handler: Handler) -> Handler:
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await db(_current_user, update.effective_user.id) is None:
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
    result = await db(wl.add_symbols, update.effective_user.id, context.args)
    today = await db(wl.latest_trading_day)
    await reply(update, messages.add_result(result, today))


@allowed
async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    removed, missing = await db(wl.remove_symbols, update.effective_user.id, context.args or [])
    await reply(update, messages.remove_result(removed, missing))


@allowed
async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    statuses = await db(wl.watchlist, update.effective_user.id)
    today = await db(wl.latest_trading_day)
    await reply(update, messages.watchlist(statuses, today))


async def _set_access(update: Update, context: ContextTypes.DEFAULT_TYPE, allow: bool) -> None:
    if not context.args or not context.args[0].isdigit():
        await reply(update, f"Usage: /{'allow' if allow else 'revoke'} TELEGRAM_ID")
        return
    user_id = int(context.args[0])
    user = await db(wl.set_allowed, user_id, allow)
    if user is None:
        await reply(update, "No such user (they need to send /start first), or it's the admin.")
        return
    await reply(update, f"{'Allowed' if allow else 'Revoked'} <code>{user_id}</code>.")
    if allow:
        await context.bot.send_message(user.chat_id, "You're in! " + messages.HELP, parse_mode=ParseMode.HTML)


@admin_only
async def allow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_access(update, context, True)


@admin_only
async def revoke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_access(update, context, False)


@admin_only
async def users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, messages.users(await db(wl.list_users)))


def _status(s) -> str:
    latest = wl.latest_trading_day(s)
    watched = s.scalar(select(func.count(func.distinct(WatchlistEntry.symbol))))
    return f"Latest trading day loaded: {messages.day(latest) if latest else 'none'}\nStocks watched: {watched}"


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


def build_application() -> Application:
    token = get_settings().telegram_bot_token
    if token is None:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set (see .env.example)")
    app = Application.builder().token(token.get_secret_value()).post_init(_post_init).build()
    for name, handler in [
        ("start", start),
        ("help", help_command),
        ("add", add),
        ("remove", remove),
        ("list", list_command),
        ("allow", allow),
        ("revoke", revoke),
        ("users", users),
        ("status", status),
    ]:
        app.add_handler(CommandHandler(name, handler))
    return app


def run() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)  # every poll is logged otherwise
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)
