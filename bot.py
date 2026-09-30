"""Telegram bot for a restaurant: menu, info and table bookings.

Customers are answered in their phone's language (it, en, fr, de, es) or the one
they pick with /lingua; the owner's messages use the dashboard's language.
All texts live in locales/<language>.json.
"""

import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta
from functools import wraps
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import InvalidToken
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import database as db
import i18n
from i18n import t, tn

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID", "").strip()
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Rome"))

MENU_PATH = Path(__file__).parent / "menu.json"

# How far in advance a table can be booked.
MAX_DAYS_AHEAD = 60
MAX_NOTES_LENGTH = 300
# Telegram rejects messages longer than 4096 characters; keep some margin.
MAX_MESSAGE_LENGTH = 4000

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
# httpx logs every polling request at INFO level: too noisy.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("restaurant-bot")

# Conversation states
NAME, PEOPLE, DATE, TIME, NOTES, CONFIRM = range(6)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_config_cache: dict | None = None
_config_stamp: tuple | None = None


def load_config() -> dict:
    """Return menu.json, re-reading it whenever the file changes (no restart needed).

    If the file is broken (e.g. a JSON typo), keep using the last valid version.
    """
    global _config_cache, _config_stamp
    try:
        stat = MENU_PATH.stat()
        stamp = (str(MENU_PATH), stat.st_mtime_ns, stat.st_size)
        if stamp != _config_stamp or _config_cache is None:
            with open(MENU_PATH, encoding="utf-8") as f:
                _config_cache = json.load(f)
            _config_stamp = stamp
    except (OSError, json.JSONDecodeError) as exc:
        if _config_cache is None:
            raise
        logger.error("Cannot reload %s, using last valid version: %s", MENU_PATH.name, exc)
    return _config_cache


def now() -> datetime:
    return datetime.now(TIMEZONE)


def user_language(update: Update) -> str:
    """Language chosen with /lingua, else the phone's language, else the restaurant default."""
    user = update.effective_user
    return (
        db.get_user_language(user.id)
        or i18n.normalize(getattr(user, "language_code", None))
        or i18n.default_language()
    )


def owner_language() -> str:
    """The owner reads notifications in the dashboard's language."""
    return i18n.panel_language(load_config())


def main_keyboard(lang: str) -> ReplyKeyboardMarkup:
    b = lambda key: t(lang, f"bot.btn.{key}")  # noqa: E731
    return ReplyKeyboardMarkup([[b("menu"), b("book")], [b("info"), b("my_bookings")]], resize_keyboard=True)


def cancel_keyboard(lang: str, rows: list[list[str]] | None = None) -> ReplyKeyboardMarkup:
    """Step keyboard with a cancel button always at the bottom."""
    return ReplyKeyboardMarkup((rows or []) + [[t(lang, "bot.btn.cancel")]], resize_keyboard=True)


def is_label(text: str, key: str) -> bool:
    """True if text is the label of button `key` in any language."""
    return text.strip().lower() in {label.lower() for label in i18n.all_labels(key)}


def parse_date(text: str, today: date) -> date | None:
    """Parse the Today/Tomorrow buttons (any language), 'dd/mm' or 'dd/mm/yyyy' (also with '-' or '.')."""
    if is_label(text, "bot.btn.today"):
        return today
    if is_label(text, "bot.btn.tomorrow"):
        return today + timedelta(days=1)

    match = re.fullmatch(r"(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2}|\d{4}))?", text.strip())
    if not match:
        return None
    day, month, year = match.groups()
    try:
        if year:
            year = int(year) + (2000 if len(year) == 2 else 0)
            return date(year, int(month), int(day))
        # No year given: pick the next occurrence of that day.
        candidate = date(today.year, int(month), int(day))
        if candidate < today:
            candidate = date(today.year + 1, int(month), int(day))
        return candidate
    except ValueError:
        return None


def normalize_time(text: str) -> str | None:
    """Turn '20', '20.00', '20:00' into '20:00'."""
    match = re.fullmatch(r"(\d{1,2})(?:[:.](\d{2}))?", text.strip())
    if not match:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2) or 0)
    if hours > 23 or minutes > 59:
        return None
    return f"{hours:02d}:{minutes:02d}"


def available_times(booking_date: date, config: dict) -> list[str]:
    """Time slots still bookable for the given date (past slots of today are excluded)."""
    slots = config["restaurant"]["booking_times"]
    current = now()
    if booking_date != current.date():
        return slots
    return [s for s in slots if s > current.strftime("%H:%M")]


def booking_summary(data: dict, lang: str) -> str:
    return t(
        lang, "bot.summary",
        name=escape(data["name"]),
        people=data["people"],
        date=i18n.format_date(data["date"], lang),
        time=data["time"],
        notes=escape(data["notes"]) if data.get("notes") else "—",
    )


async def notify_owner(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Send a message to the restaurant owner, if OWNER_CHAT_ID is configured."""
    if not OWNER_CHAT_ID:
        logger.warning("OWNER_CHAT_ID not set: owner notification skipped")
        return
    try:
        await context.bot.send_message(OWNER_CHAT_ID, text, parse_mode=ParseMode.HTML)
    except Exception:
        # Typically the owner never started the bot, or the chat id is wrong.
        logger.exception("Could not notify owner (chat id %s)", OWNER_CHAT_ID)


def user_mention(update: Update) -> str:
    user = update.effective_user
    if user.username:
        return f"@{escape(user.username)}"
    return f'<a href="tg://user?id={user.id}">{escape(user.full_name)}</a>'


# --------------------------------------------------------------------------- #
# Basic commands
# --------------------------------------------------------------------------- #


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    lang = user_language(update)
    await update.message.reply_text(
        t(lang, "bot.start",
          name=escape(update.effective_user.first_name or ""),
          restaurant=escape(load_config()["restaurant"]["name"])),
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(lang),
    )
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = user_language(update)
    text = t(lang, "bot.help", **{k: t(lang, f"bot.btn.{k}") for k in ("menu", "book", "info", "my_bookings")})
    if is_owner(update):
        text += t(lang, "bot.help_owner")
    await update.message.reply_text(text, reply_markup=main_keyboard(lang))


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        t(user_language(update), "bot.my_id", chat_id=update.effective_chat.id),
        parse_mode=ParseMode.HTML,
    )


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = user_language(update)
    config = load_config()
    # Dish and category names come from menu.json and are not translated.
    parts = [t(lang, "bot.menu_title", restaurant=escape(config["restaurant"]["name"]))]
    for section in config["menu"]:
        lines = [f"\n{section.get('emoji', '')} <b>{escape(section['category'].upper())}</b>"]
        for item in section["items"]:
            lines.append(f"• {escape(item['name'])} — <b>{i18n.format_price(item['price'], lang)}</b>")
            if item.get("description"):
                lines.append(f"   <i>{escape(item['description'])}</i>")
        parts.append("\n".join(lines))
    parts.append("\n" + t(lang, "bot.menu_footer"))
    await update.message.reply_text(
        "\n".join(parts), parse_mode=ParseMode.HTML, reply_markup=main_keyboard(lang)
    )


async def show_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = user_language(update)
    info = load_config()["restaurant"]
    hours = "\n".join(f"• {escape(h)}" for h in info["opening_hours"])
    text = (
        f"🍽 <b>{escape(info['name'])}</b>\n\n"
        f"🕐 <b>{t(lang, 'bot.info_hours')}</b>\n{hours}\n\n"
        f"📍 <b>{t(lang, 'bot.info_address')}</b>\n{escape(info['address'])}\n"
    )
    if info.get("maps_url"):
        text += f'<a href="{escape(info["maps_url"])}">{t(lang, "bot.info_maps")}</a>\n'
    if info.get("phone"):
        text += f"\n📞 <b>{t(lang, 'bot.info_phone')}</b>\n{escape(info['phone'])}"
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(lang),
        disable_web_page_preview=True,
    )


# --- Language ---


async def choose_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = user_language(update)
    buttons = [
        [InlineKeyboardButton(t(code, "language_name") + (" ✓" if code == lang else ""), callback_data=f"lang:{code}")]
        for code in i18n.SUPPORTED
    ]
    await update.message.reply_text(t(lang, "bot.language_ask"), reply_markup=InlineKeyboardMarkup(buttons))


async def set_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    lang = query.data.split(":")[1]
    if lang not in i18n.SUPPORTED:
        await query.answer()
        return
    db.set_user_language(update.effective_user.id, lang)
    logger.info("User %s chose language %s", update.effective_user.id, lang)
    await query.answer()
    await query.edit_message_text(t(lang, "language_name"))
    # A new message carries the main keyboard in the new language.
    await context.bot.send_message(
        update.effective_chat.id, t(lang, "bot.language_set"), reply_markup=main_keyboard(lang)
    )


# --------------------------------------------------------------------------- #
# Booking conversation
# --------------------------------------------------------------------------- #


async def booking_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = user_language(update)
    context.user_data["booking"] = {}
    first_name = update.effective_user.first_name
    await update.message.reply_text(
        t(lang, "bot.book_start"),
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_keyboard(lang, [[first_name]] if first_name else None),
    )
    return NAME


async def booking_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = user_language(update)
    name = update.message.text.strip()
    if not 2 <= len(name) <= 50:
        await update.message.reply_text(t(lang, "bot.name_invalid"))
        return NAME
    context.user_data["booking"]["name"] = name

    await update.message.reply_text(
        t(lang, "bot.ask_people"),
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_keyboard(lang, [["1", "2", "3", "4"], ["5", "6", "7", "8"]]),
    )
    return PEOPLE


async def booking_people(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = user_language(update)
    max_people = load_config()["restaurant"].get("max_people", 20)
    text = update.message.text.strip()
    if not text.isdigit() or not 1 <= int(text) <= max_people:
        await update.message.reply_text(t(lang, "bot.people_invalid", max=max_people))
        return PEOPLE
    context.user_data["booking"]["people"] = int(text)

    today, tomorrow = t(lang, "bot.btn.today"), t(lang, "bot.btn.tomorrow")
    await update.message.reply_text(
        t(lang, "bot.ask_date", today=today, tomorrow=tomorrow),
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_keyboard(lang, [[today, tomorrow]]),
    )
    return DATE


async def booking_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = user_language(update)
    config = load_config()
    today = now().date()
    chosen = parse_date(update.message.text, today)

    if chosen is None:
        await update.message.reply_text(t(lang, "bot.date_invalid"), parse_mode=ParseMode.HTML)
        return DATE
    if chosen < today:
        await update.message.reply_text(t(lang, "bot.date_past"))
        return DATE
    if chosen > today + timedelta(days=MAX_DAYS_AHEAD):
        await update.message.reply_text(t(lang, "bot.date_too_far", days=MAX_DAYS_AHEAD))
        return DATE
    if chosen.weekday() in config["restaurant"].get("closed_weekdays", []):
        await update.message.reply_text(t(lang, "bot.date_closed", weekday=i18n.weekday_name(chosen, lang)))
        return DATE

    slots = available_times(chosen, config)
    if not slots:
        await update.message.reply_text(
            t(lang, "bot.no_slots_today"),
            reply_markup=cancel_keyboard(lang, [[t(lang, "bot.btn.tomorrow")]]),
        )
        return DATE

    context.user_data["booking"]["date"] = chosen
    # Show time slots two per row.
    rows = [slots[i : i + 2] for i in range(0, len(slots), 2)]
    await update.message.reply_text(
        t(lang, "bot.ask_time", date=i18n.format_date(chosen, lang)),
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_keyboard(lang, rows),
    )
    return TIME


async def booking_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = user_language(update)
    slots = available_times(context.user_data["booking"]["date"], load_config())
    chosen = normalize_time(update.message.text)
    if chosen not in slots:
        await update.message.reply_text(t(lang, "bot.time_invalid", slots=", ".join(slots)))
        return TIME
    context.user_data["booking"]["time"] = chosen

    no_notes = t(lang, "bot.btn.no_notes")
    await update.message.reply_text(
        t(lang, "bot.ask_notes", no_notes=no_notes),
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_keyboard(lang, [[no_notes]]),
    )
    return NOTES


async def booking_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = user_language(update)
    text = update.message.text.strip()
    if len(text) > MAX_NOTES_LENGTH:
        await update.message.reply_text(t(lang, "bot.notes_too_long", max=MAX_NOTES_LENGTH))
        return NOTES
    context.user_data["booking"]["notes"] = None if is_label(text, "bot.btn.no_notes") else text

    await update.message.reply_text(
        t(lang, "bot.confirm_question", summary=booking_summary(context.user_data["booking"], lang)),
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardMarkup(
            [[t(lang, "bot.btn.confirm")], [t(lang, "bot.btn.restart"), t(lang, "bot.btn.cancel")]],
            resize_keyboard=True,
        ),
    )
    return CONFIRM


async def booking_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = user_language(update)
    data = context.user_data.pop("booking")
    user = update.effective_user

    # The chosen slot may have expired while the user was typing the notes.
    if data["time"] not in available_times(data["date"], load_config()):
        await update.message.reply_text(t(lang, "bot.slot_gone"), reply_markup=main_keyboard(lang))
        return ConversationHandler.END

    booking_id = db.add_booking(
        user_id=user.id,
        username=user.username,
        name=data["name"],
        people=data["people"],
        booking_date=data["date"],
        booking_time=data["time"],
        notes=data.get("notes"),
        language=lang,
    )
    logger.info("New booking #%s by user %s (%s)", booking_id, user.id, lang)

    await update.message.reply_text(
        t(lang, "bot.confirmed", id=booking_id, summary=booking_summary(data, lang),
          my_bookings=t(lang, "bot.btn.my_bookings")),
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(lang),
    )
    owner = owner_language()
    await notify_owner(
        context,
        t(owner, "owner.new_booking", id=booking_id, summary=booking_summary(data, owner), user=user_mention(update)),
    )
    return ConversationHandler.END


async def booking_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await booking_start(update, context)


async def booking_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = user_language(update)
    context.user_data.pop("booking", None)
    await update.message.reply_text(t(lang, "bot.flow_cancelled"), reply_markup=main_keyboard(lang))
    return ConversationHandler.END


async def booking_unexpected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Anything the current step can't handle (stickers, photos, unknown commands...)."""
    await update.message.reply_text(t(user_language(update), "bot.unexpected"))
    # Returning None keeps the conversation in the current state.


# --------------------------------------------------------------------------- #
# My bookings
# --------------------------------------------------------------------------- #


async def my_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = user_language(update)
    bookings = db.get_upcoming_bookings(update.effective_user.id, now().date())
    if not bookings:
        await update.message.reply_text(
            t(lang, "bot.my_none", book=t(lang, "bot.btn.book")), reply_markup=main_keyboard(lang)
        )
        return

    lines = [t(lang, "bot.my_title")]
    buttons = []
    for b in bookings:
        d = date.fromisoformat(b["date"])
        lines.append(t(lang, "bot.my_item", id=b["id"], date=i18n.format_date(d, lang), time=b["time"],
                       people=tn(lang, "common.people", b["people"]), name=escape(b["name"])))
        buttons.append([InlineKeyboardButton(
            t(lang, "bot.my_cancel_button", id=b["id"], day=d.strftime("%d/%m"), time=b["time"]),
            callback_data=f"cancel:{b['id']}",
        )])
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons)
    )


async def cancel_booking_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """First tap on 'Cancel': ask for confirmation."""
    lang = user_language(update)
    query = update.callback_query
    booking_id = int(query.data.split(":")[1])
    booking = db.get_booking(booking_id)

    if not booking or booking["user_id"] != update.effective_user.id or booking["status"] != "confirmed":
        await query.answer(t(lang, "bot.not_found"), show_alert=True)
        return
    await query.answer()
    d = date.fromisoformat(booking["date"])
    await query.edit_message_text(
        t(lang, "bot.cancel_ask", id=booking_id, date=i18n.format_date(d, lang), time=booking["time"]),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(t(lang, "bot.cancel_yes"), callback_data=f"cancel_yes:{booking_id}"),
            InlineKeyboardButton(t(lang, "bot.cancel_no"), callback_data="cancel_no"),
        ]]),
    )


async def cancel_booking_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = user_language(update)
    query = update.callback_query
    booking_id = int(query.data.split(":")[1])
    booking = db.get_booking(booking_id)

    if not db.cancel_booking(booking_id, update.effective_user.id):
        await query.answer(t(lang, "bot.not_found"), show_alert=True)
        return
    await query.answer(t(lang, "bot.cancel_answer"))
    logger.info("Booking #%s cancelled by user %s", booking_id, update.effective_user.id)

    d = date.fromisoformat(booking["date"])
    await query.edit_message_text(
        t(lang, "bot.cancel_done", id=booking_id, date=i18n.format_date(d, lang), time=booking["time"])
    )
    owner = owner_language()
    await notify_owner(
        context,
        t(owner, "owner.cancelled_booking", id=booking_id, name=escape(booking["name"]),
          people=tn(owner, "common.people", booking["people"]), date=i18n.format_date(d, owner),
          time=booking["time"], user=user_mention(update)),
    )


async def cancel_booking_abort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(t(user_language(update), "bot.cancel_kept"))


# --------------------------------------------------------------------------- #
# Owner commands (always in the dashboard's language)
# --------------------------------------------------------------------------- #


def is_owner(update: Update) -> bool:
    return bool(OWNER_CHAT_ID) and str(update.effective_chat.id) == OWNER_CHAT_ID


def owner_only(handler):
    """Decorator: run the handler only for the owner's chat."""

    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_owner(update):
            logger.info("User %s tried owner command %s", update.effective_user.id, update.message.text)
            lang = user_language(update)
            await update.message.reply_text(t(lang, "bot.owner_only"), reply_markup=main_keyboard(lang))
            return
        await handler(update, context)

    return wrapper


def format_owner_booking(b, lang: str) -> str:
    """One booking as seen by the owner: time, name, people, notes."""
    line = t(lang, "owner.booking_line", time=b["time"], name=escape(b["name"]), people=b["people"], id=b["id"])
    if b["source"] == "manual":
        # Added from the dashboard, e.g. a phone call.
        line += f"\n      📞 {escape(b['phone'] or t(lang, 'owner.by_phone'))}"
    if b["notes"]:
        line += f"\n      📝 {escape(b['notes'])}"
    return line


async def reply_long(update: Update, text: str) -> None:
    """Send text in several messages if it exceeds Telegram's 4096-character limit."""
    # Split on line boundaries so HTML tags (always opened and closed on one line) stay intact.
    chunk = ""
    for line in text.split("\n"):
        if chunk and len(chunk) + len(line) + 1 > MAX_MESSAGE_LENGTH:
            await update.message.reply_text(chunk.strip(), parse_mode=ParseMode.HTML)
            chunk = ""
        chunk += line + "\n"
    await update.message.reply_text(chunk.strip(), parse_mode=ParseMode.HTML)


@owner_only
async def owner_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = owner_language()
    today = now().date()
    bookings = db.get_bookings_between(today, today)
    header = t(lang, "owner.today_title", date=i18n.format_date(today, lang))
    if not bookings:
        await update.message.reply_text(f"{header}\n\n{t(lang, 'owner.today_none')}", parse_mode=ParseMode.HTML)
        return

    covers = sum(b["people"] for b in bookings)
    total = t(lang, "owner.today_total", covers=tn(lang, "owner.covers", covers),
              bookings=tn(lang, "owner.bookings", len(bookings)))
    await reply_long(update, "\n\n".join([header] + [format_owner_booking(b, lang) for b in bookings] + [total]))


@owner_only
async def owner_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = owner_language()
    today = now().date()
    last_day = today + timedelta(days=6)
    bookings = db.get_bookings_between(today, last_day)
    header = t(lang, "owner.week_title", start=today.strftime("%d/%m"), end=last_day.strftime("%d/%m"))
    if not bookings:
        await update.message.reply_text(f"{header}\n\n{t(lang, 'owner.week_none')}", parse_mode=ParseMode.HTML)
        return

    by_day: dict[str, list] = {}
    for b in bookings:
        by_day.setdefault(b["date"], []).append(b)

    blocks = [header]
    for day, day_bookings in by_day.items():
        covers = sum(b["people"] for b in day_bookings)
        lines = [f"📅 <b>{i18n.ucfirst(i18n.format_date(date.fromisoformat(day), lang))}</b>"]
        lines += [format_owner_booking(b, lang) for b in day_bookings]
        lines.append(t(lang, "owner.day_total", covers=tn(lang, "owner.covers", covers)))
        blocks.append("\n".join(lines))

    total = sum(b["people"] for b in bookings)
    blocks.append(t(lang, "owner.week_total", covers=tn(lang, "owner.covers", total),
                    bookings=tn(lang, "owner.bookings", len(bookings))))
    await reply_long(update, "\n\n".join(blocks))


# --------------------------------------------------------------------------- #
# Fallbacks and errors
# --------------------------------------------------------------------------- #


async def nothing_to_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = user_language(update)
    await update.message.reply_text(
        t(lang, "bot.nothing_to_cancel", my_bookings=t(lang, "bot.btn.my_bookings")),
        reply_markup=main_keyboard(lang),
    )


async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = user_language(update)
    await update.message.reply_text(t(lang, "bot.unknown"), reply_markup=main_keyboard(lang))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled error while processing an update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            lang = user_language(update) if update.effective_user else i18n.default_language()
            await update.effective_message.reply_text(t(lang, "bot.error"), reply_markup=main_keyboard(lang))
        except Exception:
            logger.exception("Could not send error message to user")


def button(key: str) -> filters.MessageFilter:
    """Filter matching a keyboard button's text in any supported language."""
    return filters.Regex(i18n.labels_regex(f"bot.btn.{key}"))


CUSTOMER_COMMANDS = ("start", "prenota", "menu", "info", "prenotazioni", "annulla", "lingua", "mioid", "help")
# Every command also answers to its international name (/book, /stop...); each language
# shows its own names in the '/' menu and in the texts (locales: bot.command_names).
ALIASES = {"prenota": "book", "prenotazioni": "bookings", "annulla": "stop", "lingua": "language",
           "mioid": "myid", "oggi": "today", "settimana": "week"}


def names(command: str) -> list[str]:
    return [command, ALIASES[command]] if command in ALIASES else [command]


def command_list(lang: str, owner: bool = False) -> list[BotCommand]:
    commands = []
    for name in (("oggi", "settimana") if owner else ()) + CUSTOMER_COMMANDS:
        shown = i18n.raw(lang, "bot.command_names").get(name, name)
        commands.append(BotCommand(shown, t(lang, f"bot.commands.{name}")))
    return commands


async def post_init(application: Application) -> None:
    """Register the '/' menu: descriptions in each supported language."""
    bot = application.bot
    await bot.set_my_commands(command_list(i18n.default_language()))
    for lang in i18n.SUPPORTED:
        await bot.set_my_commands(command_list(lang), language_code=lang)

    # The owner also sees the reserved commands, in the dashboard's language.
    if OWNER_CHAT_ID:
        try:
            await bot.set_my_commands(command_list(owner_language(), owner=True), scope=BotCommandScopeChat(OWNER_CHAT_ID))
        except Exception:
            # Fails if the owner has never written to the bot: not critical.
            logger.warning("Could not set the owner's command menu (chat id %s)", OWNER_CHAT_ID)


def build_application() -> Application:
    application = Application.builder().token(TOKEN).post_init(post_init).build()

    text = filters.TEXT & ~filters.COMMAND
    cancel = [CommandHandler(names("annulla"), booking_cancel), MessageHandler(button("cancel"), booking_cancel)]

    booking_conversation = ConversationHandler(
        entry_points=[
            CommandHandler(names("prenota"), booking_start),
            MessageHandler(button("book"), booking_start),
        ],
        states={
            NAME: [*cancel, MessageHandler(text, booking_name)],
            PEOPLE: [*cancel, MessageHandler(text, booking_people)],
            DATE: [*cancel, MessageHandler(text, booking_date)],
            TIME: [*cancel, MessageHandler(text, booking_time)],
            NOTES: [*cancel, MessageHandler(text, booking_notes)],
            CONFIRM: [
                *cancel,
                MessageHandler(button("confirm"), booking_confirm),
                MessageHandler(button("restart"), booking_restart),
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            MessageHandler(filters.ALL, booking_unexpected),
        ],
    )

    application.add_handler(booking_conversation)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler(names("mioid"), my_id))
    application.add_handler(CommandHandler(names("lingua"), choose_language))
    application.add_handler(CommandHandler("menu", show_menu))
    application.add_handler(CommandHandler("info", show_info))
    application.add_handler(CommandHandler(names("prenotazioni"), my_bookings))
    application.add_handler(CommandHandler(names("oggi"), owner_today))
    application.add_handler(CommandHandler(names("settimana"), owner_week))
    application.add_handler(MessageHandler(button("menu"), show_menu))
    application.add_handler(MessageHandler(button("info"), show_info))
    application.add_handler(MessageHandler(button("my_bookings"), my_bookings))
    application.add_handler(CallbackQueryHandler(set_language, pattern=r"^lang:[a-z]{2}$"))
    application.add_handler(CallbackQueryHandler(cancel_booking_ask, pattern=r"^cancel:\d+$"))
    application.add_handler(CallbackQueryHandler(cancel_booking_confirm, pattern=r"^cancel_yes:\d+$"))
    application.add_handler(CallbackQueryHandler(cancel_booking_abort, pattern=r"^cancel_no$"))
    application.add_handler(CommandHandler(names("annulla"), nothing_to_cancel))
    # Unknown commands, free text, stickers, photos...
    application.add_handler(MessageHandler(filters.ALL, unknown_message))
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    if not TOKEN:
        sys.exit("TELEGRAM_TOKEN is missing. Copy .env.example to .env and set your bot token.")
    load_config()  # fail fast if menu.json is broken
    db.init_db()
    if not OWNER_CHAT_ID:
        logger.warning("OWNER_CHAT_ID not set: the owner will not receive booking notifications")

    logger.info("Bot started (default language: %s). Press Ctrl+C to stop.", i18n.default_language())
    try:
        build_application().run_polling(
            # Only new messages and button taps: edited messages are ignored on purpose.
            allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY],
        )
    except InvalidToken:
        sys.exit("Telegram rejected TELEGRAM_TOKEN. Check the token in your .env file.")


if __name__ == "__main__":
    main()
