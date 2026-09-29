"""Telegram bot for a restaurant: menu, info and table bookings."""

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
    KeyboardButton,
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

# Main keyboard buttons
BTN_MENU = "📖 Menù"
BTN_BOOK = "📅 Prenota un tavolo"
BTN_INFO = "📍 Orari e indirizzo"
BTN_MY_BOOKINGS = "🗂 Le mie prenotazioni"

# Booking flow buttons
BTN_CANCEL = "❌ Annulla"
BTN_TODAY = "Oggi"
BTN_TOMORROW = "Domani"
BTN_NO_NOTES = "Nessuna nota"
BTN_CONFIRM = "✅ Conferma"
BTN_RESTART = "🔄 Ricomincia"

# Conversation states
NAME, PEOPLE, DATE, TIME, NOTES, CONFIRM = range(6)

WEEKDAYS_IT = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
MONTHS_IT = [
    "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
    "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre",
]

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_MENU, BTN_BOOK], [BTN_INFO, BTN_MY_BOOKINGS]],
    resize_keyboard=True,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_config_cache: dict | None = None


def load_config() -> dict:
    """Load menu.json on every call so edits apply without restarting the bot.

    If the file is broken (e.g. a JSON typo), keep using the last valid version.
    """
    global _config_cache
    try:
        with open(MENU_PATH, encoding="utf-8") as f:
            _config_cache = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        if _config_cache is None:
            raise
        logger.error("Cannot reload %s, using last valid version: %s", MENU_PATH.name, exc)
    return _config_cache


def now() -> datetime:
    return datetime.now(TIMEZONE)


def format_date(d: date) -> str:
    """e.g. 'venerdì 3 ottobre 2026'"""
    return f"{WEEKDAYS_IT[d.weekday()]} {d.day} {MONTHS_IT[d.month - 1]} {d.year}"


def format_price(price: float) -> str:
    return f"€ {price:.2f}".replace(".", ",")


def parse_date(text: str, today: date) -> date | None:
    """Parse 'oggi', 'domani', 'dd/mm' or 'dd/mm/yyyy' (also with '-' or '.')."""
    text = text.strip().lower()
    if text == BTN_TODAY.lower():
        return today
    if text == BTN_TOMORROW.lower():
        return today + timedelta(days=1)

    match = re.fullmatch(r"(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2}|\d{4}))?", text)
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


def booking_summary(data: dict) -> str:
    notes = escape(data["notes"]) if data.get("notes") else "—"
    return (
        f"👤 <b>Nome:</b> {escape(data['name'])}\n"
        f"👥 <b>Persone:</b> {data['people']}\n"
        f"📅 <b>Data:</b> {format_date(data['date'])}\n"
        f"🕗 <b>Orario:</b> {data['time']}\n"
        f"📝 <b>Note:</b> {notes}"
    )


def cancel_keyboard(rows: list[list[str]] | None = None) -> ReplyKeyboardMarkup:
    """Step keyboard with an 'Annulla' button always at the bottom."""
    return ReplyKeyboardMarkup((rows or []) + [[BTN_CANCEL]], resize_keyboard=True)


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
    config = load_config()
    first_name = escape(update.effective_user.first_name or "")
    await update.message.reply_text(
        f"Ciao {first_name}! 👋\n"
        f"Benvenuto da <b>{escape(config['restaurant']['name'])}</b>.\n\n"
        "Da qui puoi consultare il menù, prenotare un tavolo e gestire le tue prenotazioni.\n"
        "Scegli un'opzione qui sotto 👇",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Ecco cosa posso fare:\n\n"
        f"{BTN_MENU} – consulta il menù\n"
        f"{BTN_BOOK} – prenota in pochi passaggi\n"
        f"{BTN_INFO} – dove siamo e quando siamo aperti\n"
        f"{BTN_MY_BOOKINGS} – vedi o cancella le tue prenotazioni\n\n"
        "Comandi: /start, /prenota, /menu, /info, /prenotazioni, /annulla, /mioid"
    )
    if is_owner(update):
        text += (
            "\n\n👨‍🍳 Comandi titolare:\n"
            "/oggi – prenotazioni di oggi\n"
            "/settimana – prenotazioni dei prossimi 7 giorni"
        )
    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"Il tuo chat_id è: <code>{update.effective_chat.id}</code>\n\n"
        "Se sei il titolare, copialo nel file <code>.env</code> come "
        "<code>OWNER_CHAT_ID</code> e riavvia il bot per ricevere le notifiche delle prenotazioni.",
        parse_mode=ParseMode.HTML,
    )


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = load_config()
    parts = [f"📖 <b>Menù – {escape(config['restaurant']['name'])}</b>"]
    for section in config["menu"]:
        lines = [f"\n{section.get('emoji', '')} <b>{escape(section['category'].upper())}</b>"]
        for item in section["items"]:
            lines.append(f"• {escape(item['name'])} — <b>{format_price(item['price'])}</b>")
            if item.get("description"):
                lines.append(f"   <i>{escape(item['description'])}</i>")
        parts.append("\n".join(lines))
    parts.append("\n<i>Per allergeni e intolleranze chiedi al nostro personale.</i>")
    await update.message.reply_text(
        "\n".join(parts), parse_mode=ParseMode.HTML, reply_markup=MAIN_KEYBOARD
    )


async def show_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    info = load_config()["restaurant"]
    hours = "\n".join(f"• {escape(h)}" for h in info["opening_hours"])
    text = (
        f"🍽 <b>{escape(info['name'])}</b>\n\n"
        f"🕐 <b>Orari</b>\n{hours}\n\n"
        f"📍 <b>Indirizzo</b>\n{escape(info['address'])}\n"
    )
    if info.get("maps_url"):
        text += f'<a href="{escape(info["maps_url"])}">Apri in Google Maps</a>\n'
    if info.get("phone"):
        text += f"\n📞 <b>Telefono</b>\n{escape(info['phone'])}"
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_KEYBOARD,
        disable_web_page_preview=True,
    )


# --------------------------------------------------------------------------- #
# Booking conversation
# --------------------------------------------------------------------------- #


async def booking_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["booking"] = {}
    first_name = update.effective_user.first_name
    rows = [[first_name]] if first_name else None
    await update.message.reply_text(
        "Perfetto, prenotiamo un tavolo! 🍽\n"
        "Puoi annullare in qualsiasi momento con /annulla.\n\n"
        "<b>1/5</b> – A che nome prenotiamo?",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_keyboard(rows),
    )
    return NAME


async def booking_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    if not 2 <= len(name) <= 50:
        await update.message.reply_text("Inserisci un nome valido (tra 2 e 50 caratteri).")
        return NAME
    context.user_data["booking"]["name"] = name

    await update.message.reply_text(
        "<b>2/5</b> – Per quante persone?\n"
        "Scegli un numero o scrivilo.",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_keyboard([["1", "2", "3", "4"], ["5", "6", "7", "8"]]),
    )
    return PEOPLE


async def booking_people(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    max_people = load_config()["restaurant"].get("max_people", 20)
    text = update.message.text.strip()
    if not text.isdigit() or not 1 <= int(text) <= max_people:
        await update.message.reply_text(
            f"Scrivi un numero di persone tra 1 e {max_people}.\n"
            "Per gruppi più numerosi contattaci per telefono 📞"
        )
        return PEOPLE
    context.user_data["booking"]["people"] = int(text)

    await update.message.reply_text(
        "<b>3/5</b> – Per quale giorno?\n"
        "Scegli <i>Oggi</i> o <i>Domani</i>, oppure scrivi una data (es. <code>25/12</code>).",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_keyboard([[BTN_TODAY, BTN_TOMORROW]]),
    )
    return DATE


async def booking_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    config = load_config()
    today = now().date()
    chosen = parse_date(update.message.text, today)

    if chosen is None:
        await update.message.reply_text(
            "Non ho capito la data 🤔\n"
            "Scrivila nel formato <code>gg/mm</code> o <code>gg/mm/aaaa</code>, es. <code>25/12</code>.",
            parse_mode=ParseMode.HTML,
        )
        return DATE
    if chosen < today:
        await update.message.reply_text("Questa data è già passata. Scegline un'altra 🙂")
        return DATE
    if chosen > today + timedelta(days=MAX_DAYS_AHEAD):
        await update.message.reply_text(
            f"Accettiamo prenotazioni fino a {MAX_DAYS_AHEAD} giorni in anticipo. Scegli una data più vicina."
        )
        return DATE
    if chosen.weekday() in config["restaurant"].get("closed_weekdays", []):
        await update.message.reply_text(
            f"Ci dispiace, di {WEEKDAYS_IT[chosen.weekday()]} siamo chiusi 😔 Scegli un altro giorno."
        )
        return DATE

    slots = available_times(chosen, config)
    if not slots:
        await update.message.reply_text(
            "Per oggi non ci sono più orari disponibili. Scegli un altro giorno.",
            reply_markup=cancel_keyboard([[BTN_TOMORROW]]),
        )
        return DATE

    context.user_data["booking"]["date"] = chosen
    # Show time slots two per row.
    rows = [slots[i : i + 2] for i in range(0, len(slots), 2)]
    await update.message.reply_text(
        f"Hai scelto <b>{format_date(chosen)}</b>.\n\n<b>4/5</b> – A che ora?",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_keyboard(rows),
    )
    return TIME


async def booking_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    slots = available_times(context.user_data["booking"]["date"], load_config())
    chosen = normalize_time(update.message.text)
    if chosen not in slots:
        await update.message.reply_text(
            "Scegli uno degli orari disponibili con i bottoni qui sotto 👇\n"
            f"Orari: {', '.join(slots)}"
        )
        return TIME
    context.user_data["booking"]["time"] = chosen

    await update.message.reply_text(
        "<b>5/5</b> – Vuoi aggiungere una nota?\n"
        "Ad esempio allergie, seggiolone, occasione speciale… "
        "Scrivila oppure premi <i>Nessuna nota</i>.",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_keyboard([[BTN_NO_NOTES]]),
    )
    return NOTES


async def booking_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if len(text) > MAX_NOTES_LENGTH:
        await update.message.reply_text(
            f"La nota è troppo lunga (max {MAX_NOTES_LENGTH} caratteri). Prova ad accorciarla."
        )
        return NOTES
    context.user_data["booking"]["notes"] = None if text == BTN_NO_NOTES else text

    await update.message.reply_text(
        "Ecco il riepilogo della tua prenotazione:\n\n"
        f"{booking_summary(context.user_data['booking'])}\n\n"
        "Confermi?",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardMarkup(
            [[BTN_CONFIRM], [BTN_RESTART, BTN_CANCEL]], resize_keyboard=True
        ),
    )
    return CONFIRM


async def booking_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data.pop("booking")
    user = update.effective_user

    # The chosen slot may have expired while the user was typing the notes.
    if data["time"] not in available_times(data["date"], load_config()):
        await update.message.reply_text(
            "Ops, nel frattempo l'orario scelto non è più disponibile. Ricominciamo 🙏",
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    booking_id = db.add_booking(
        user_id=user.id,
        username=user.username,
        name=data["name"],
        people=data["people"],
        booking_date=data["date"],
        booking_time=data["time"],
        notes=data.get("notes"),
    )
    logger.info("New booking #%s by user %s", booking_id, user.id)

    await update.message.reply_text(
        f"🎉 <b>Prenotazione confermata!</b> (n. {booking_id})\n\n"
        f"{booking_summary(data)}\n\n"
        f"Ti aspettiamo! Se hai un imprevisto puoi cancellarla da «{BTN_MY_BOOKINGS}».",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_KEYBOARD,
    )
    await notify_owner(
        context,
        f"🔔 <b>Nuova prenotazione</b> (n. {booking_id})\n\n"
        f"{booking_summary(data)}\n\n"
        f"Da: {user_mention(update)}",
    )
    return ConversationHandler.END


async def booking_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await booking_start(update, context)


async def booking_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("booking", None)
    await update.message.reply_text(
        "Prenotazione annullata. Quando vuoi, sono qui 🙂", reply_markup=MAIN_KEYBOARD
    )
    return ConversationHandler.END


async def booking_unexpected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Anything the current step can't handle (stickers, photos, unknown commands...)."""
    await update.message.reply_text(
        "Non ho capito 🤔 Rispondi alla domanda qui sopra usando i bottoni o scrivendo un testo.\n"
        "Per interrompere la prenotazione usa /annulla."
    )
    # Returning None keeps the conversation in the current state.


# --------------------------------------------------------------------------- #
# My bookings
# --------------------------------------------------------------------------- #


async def my_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bookings = db.get_upcoming_bookings(update.effective_user.id, now().date())
    if not bookings:
        await update.message.reply_text(
            f"Non hai prenotazioni in programma.\nPremi «{BTN_BOOK}» per prenotare un tavolo!",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    lines = ["🗂 <b>Le tue prenotazioni</b>\n"]
    buttons = []
    for b in bookings:
        d = date.fromisoformat(b["date"])
        lines.append(
            f"<b>n. {b['id']}</b> – {format_date(d)} alle {b['time']}\n"
            f"   {b['people']} persone, a nome {escape(b['name'])}"
        )
        buttons.append(
            [InlineKeyboardButton(
                f"❌ Cancella n. {b['id']} ({d.strftime('%d/%m')} {b['time']})",
                callback_data=f"cancel:{b['id']}",
            )]
        )
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cancel_booking_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """First tap on 'Cancella': ask for confirmation."""
    query = update.callback_query
    booking_id = int(query.data.split(":")[1])
    booking = db.get_booking(booking_id)

    if not booking or booking["user_id"] != update.effective_user.id or booking["status"] != "confirmed":
        await query.answer("Prenotazione non trovata o già cancellata.", show_alert=True)
        return
    await query.answer()
    d = date.fromisoformat(booking["date"])
    await query.edit_message_text(
        f"Vuoi davvero cancellare la prenotazione <b>n. {booking_id}</b> "
        f"di {format_date(d)} alle {booking['time']}?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Sì, cancella", callback_data=f"cancel_yes:{booking_id}"),
            InlineKeyboardButton("No, tienila", callback_data="cancel_no"),
        ]]),
    )


async def cancel_booking_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    booking_id = int(query.data.split(":")[1])
    booking = db.get_booking(booking_id)

    if not db.cancel_booking(booking_id, update.effective_user.id):
        await query.answer("Prenotazione non trovata o già cancellata.", show_alert=True)
        return
    await query.answer("Prenotazione cancellata")
    logger.info("Booking #%s cancelled by user %s", booking_id, update.effective_user.id)

    d = date.fromisoformat(booking["date"])
    await query.edit_message_text(
        f"✅ La prenotazione n. {booking_id} di {format_date(d)} alle {booking['time']} è stata cancellata."
    )
    await notify_owner(
        context,
        f"🚫 <b>Prenotazione cancellata</b> (n. {booking_id})\n\n"
        f"👤 {escape(booking['name'])} – {booking['people']} persone\n"
        f"📅 {format_date(d)} alle {booking['time']}\n\n"
        f"Da: {user_mention(update)}",
    )


async def cancel_booking_abort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Ok, la prenotazione resta valida 👍")


# --------------------------------------------------------------------------- #
# Owner commands
# --------------------------------------------------------------------------- #


def is_owner(update: Update) -> bool:
    return bool(OWNER_CHAT_ID) and str(update.effective_chat.id) == OWNER_CHAT_ID


def owner_only(handler):
    """Decorator: run the handler only for the owner's chat."""

    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_owner(update):
            logger.info("User %s tried owner command %s", update.effective_user.id, update.message.text)
            await update.message.reply_text(
                "Questo comando non è disponibile.", reply_markup=MAIN_KEYBOARD
            )
            return
        await handler(update, context)

    return wrapper


def format_owner_booking(b) -> str:
    """One booking as seen by the owner: time, name, people, notes."""
    line = f"🕗 <b>{b['time']}</b> · {escape(b['name'])} · {b['people']} pers. <i>(n. {b['id']})</i>"
    if b["source"] == "manual":
        # Added from the dashboard, e.g. a phone call.
        line += f"\n      📞 {escape(b['phone'] or 'al telefono')}"
    if b["notes"]:
        line += f"\n      📝 {escape(b['notes'])}"
    return line


def plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


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
    today = now().date()
    bookings = db.get_bookings_between(today, today)
    header = f"📋 <b>Prenotazioni di oggi</b>\n{format_date(today)}"
    if not bookings:
        await update.message.reply_text(
            f"{header}\n\nNessuna prenotazione per oggi.", parse_mode=ParseMode.HTML
        )
        return

    covers = sum(b["people"] for b in bookings)
    await reply_long(
        update,
        "\n\n".join(
            [header]
            + [format_owner_booking(b) for b in bookings]
            + [f"👥 <b>Totale: {plural(covers, 'coperto', 'coperti')}</b> "
               f"({plural(len(bookings), 'prenotazione', 'prenotazioni')})"]
        ),
    )


@owner_only
async def owner_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    today = now().date()
    last_day = today + timedelta(days=6)
    bookings = db.get_bookings_between(today, last_day)
    header = (
        f"🗓 <b>Prenotazioni dei prossimi 7 giorni</b>\n"
        f"dal {today.strftime('%d/%m')} al {last_day.strftime('%d/%m')}"
    )
    if not bookings:
        await update.message.reply_text(
            f"{header}\n\nNessuna prenotazione in questo periodo.", parse_mode=ParseMode.HTML
        )
        return

    by_day: dict[str, list] = {}
    for b in bookings:
        by_day.setdefault(b["date"], []).append(b)

    blocks = [header]
    for day, day_bookings in by_day.items():
        covers = sum(b["people"] for b in day_bookings)
        lines = [f"📅 <b>{format_date(date.fromisoformat(day)).capitalize()}</b>"]
        lines += [format_owner_booking(b) for b in day_bookings]
        lines.append(f"👥 Totale: <b>{plural(covers, 'coperto', 'coperti')}</b>")
        blocks.append("\n".join(lines))

    total = sum(b["people"] for b in bookings)
    blocks.append(
        f"<b>Totale settimana: {plural(total, 'coperto', 'coperti')}</b> "
        f"({plural(len(bookings), 'prenotazione', 'prenotazioni')})"
    )
    await reply_long(update, "\n\n".join(blocks))


# --------------------------------------------------------------------------- #
# Fallbacks and errors
# --------------------------------------------------------------------------- #


async def nothing_to_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Non c'è nessuna prenotazione in corso da annullare.\n"
        f"Per cancellare una prenotazione già confermata vai su «{BTN_MY_BOOKINGS}».",
        reply_markup=MAIN_KEYBOARD,
    )


async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Non ho capito 🤔 Usa i bottoni qui sotto per scegliere cosa fare, "
        "oppure scrivi /help per vedere tutti i comandi.",
        reply_markup=MAIN_KEYBOARD,
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled error while processing an update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Si è verificato un errore imprevisto 😓 Riprova tra poco oppure scrivi /start.",
                reply_markup=MAIN_KEYBOARD,
            )
        except Exception:
            logger.exception("Could not send error message to user")


def button(text: str) -> filters.MessageFilter:
    """Filter matching exactly the text of a keyboard button."""
    return filters.Regex(f"^{re.escape(text)}$")


async def post_init(application: Application) -> None:
    """Register the command list shown in Telegram's '/' menu."""
    commands = [
        BotCommand("start", "Menu principale"),
        BotCommand("prenota", "Prenota un tavolo"),
        BotCommand("menu", "Consulta il menù"),
        BotCommand("info", "Orari e indirizzo"),
        BotCommand("prenotazioni", "Le mie prenotazioni"),
        BotCommand("annulla", "Annulla la prenotazione in corso"),
        BotCommand("mioid", "Mostra il tuo chat_id"),
        BotCommand("help", "Aiuto"),
    ]
    await application.bot.set_my_commands(commands)

    # The owner also sees the reserved commands in their '/' menu.
    if OWNER_CHAT_ID:
        owner_commands = [
            BotCommand("oggi", "Prenotazioni di oggi"),
            BotCommand("settimana", "Prenotazioni dei prossimi 7 giorni"),
        ]
        try:
            await application.bot.set_my_commands(
                owner_commands + commands, scope=BotCommandScopeChat(OWNER_CHAT_ID)
            )
        except Exception:
            # Fails if the owner has never written to the bot: not critical.
            logger.warning("Could not set the owner's command menu (chat id %s)", OWNER_CHAT_ID)


def build_application() -> Application:
    application = Application.builder().token(TOKEN).post_init(post_init).build()

    text = filters.TEXT & ~filters.COMMAND
    cancel = [CommandHandler("annulla", booking_cancel), MessageHandler(button(BTN_CANCEL), booking_cancel)]

    booking_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("prenota", booking_start),
            MessageHandler(button(BTN_BOOK), booking_start),
        ],
        states={
            NAME: [*cancel, MessageHandler(text, booking_name)],
            PEOPLE: [*cancel, MessageHandler(text, booking_people)],
            DATE: [*cancel, MessageHandler(text, booking_date)],
            TIME: [*cancel, MessageHandler(text, booking_time)],
            NOTES: [*cancel, MessageHandler(text, booking_notes)],
            CONFIRM: [
                *cancel,
                MessageHandler(button(BTN_CONFIRM), booking_confirm),
                MessageHandler(button(BTN_RESTART), booking_restart),
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
    application.add_handler(CommandHandler("mioid", my_id))
    application.add_handler(CommandHandler("menu", show_menu))
    application.add_handler(CommandHandler("info", show_info))
    application.add_handler(CommandHandler("prenotazioni", my_bookings))
    application.add_handler(CommandHandler("oggi", owner_today))
    application.add_handler(CommandHandler("settimana", owner_week))
    application.add_handler(MessageHandler(button(BTN_MENU), show_menu))
    application.add_handler(MessageHandler(button(BTN_INFO), show_info))
    application.add_handler(MessageHandler(button(BTN_MY_BOOKINGS), my_bookings))
    application.add_handler(CallbackQueryHandler(cancel_booking_ask, pattern=r"^cancel:\d+$"))
    application.add_handler(CallbackQueryHandler(cancel_booking_confirm, pattern=r"^cancel_yes:\d+$"))
    application.add_handler(CallbackQueryHandler(cancel_booking_abort, pattern=r"^cancel_no$"))
    application.add_handler(CommandHandler("annulla", nothing_to_cancel))
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

    logger.info("Bot started. Press Ctrl+C to stop.")
    try:
        build_application().run_polling(
            # Only new messages and button taps: edited messages are ignored on purpose.
            allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY],
        )
    except InvalidToken:
        sys.exit("Telegram rejected TELEGRAM_TOKEN. Check the token in your .env file.")


if __name__ == "__main__":
    main()
