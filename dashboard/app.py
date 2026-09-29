"""Owner dashboard: bookings overview, manual bookings and menu editor."""

import asyncio
import hashlib
import hmac
import logging
import mimetypes
import os
import secrets
from datetime import date, timedelta
from html import escape
from pathlib import Path

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

import database as db  # noqa: E402
from bot import format_date, load_config, normalize_time, now  # noqa: E402

from . import menu_editor  # noqa: E402

logger = logging.getLogger("restaurant-dashboard")

PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()

UPCOMING_DAYS = 7
SESSION_MAX_AGE = 7 * 24 * 3600  # stay logged in for a week
MAX_MANUAL_PEOPLE = 200

# The session cookie is signed with a key derived from the password (and bot token):
# changing the password logs everyone out.
SESSION_SECRET = hashlib.sha256(f"dashboard-session|{PASSWORD}|{TOKEN}".encode()).hexdigest()

HERE = Path(__file__).parent
mimetypes.add_type("font/woff2", ".woff2")  # not known to Python's mimetypes on every system
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals["format_date"] = format_date
templates.env.globals["now_hm"] = lambda: now().strftime("%H:%M")
templates.env.globals["day_iso"] = lambda offset=0: (now().date() + timedelta(days=offset)).isoformat()
templates.env.globals["booking_slots"] = lambda: load_config()["restaurant"]["booking_times"]
templates.env.filters["todate"] = date.fromisoformat

# Words in the booking notes that deserve a visible flag during service.
NOTE_TAGS = {
    "Allergie": ("allerg", "celiac", "glutine", "lattosio", "intoller", "noci", "arachidi", "crostacei", "vegan"),
    "Bambini": ("bambin", "seggiolon", "passeggino", "neonat", "bimb"),
    "Festa": ("compleann", "anniversari", "festa", "laurea", "torta"),
}


def note_tags(notes: str | None) -> list[str]:
    text = (notes or "").lower()
    return [tag for tag, words in NOTE_TAGS.items() if any(w in text for w in words)]


templates.env.filters["note_tags"] = note_tags
templates.env.filters["price_input"] = (
    lambda p: f"{p:.2f}".replace(".", ",") if isinstance(p, (int, float)) else (p or "")
)

app = FastAPI(title="Restaurant dashboard", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="dashboard_session",
    max_age=SESSION_MAX_AGE,
    same_site="lax",
)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


# --------------------------------------------------------------------------- #
# Session helpers
# --------------------------------------------------------------------------- #


class LoginRequired(Exception):
    pass


class InvalidCSRF(Exception):
    pass


@app.exception_handler(LoginRequired)
async def redirect_to_login(request: Request, exc: LoginRequired):
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(InvalidCSRF)
async def invalid_csrf(request: Request, exc: InvalidCSRF):
    flash(request, "Sessione scaduta: riprova.", "error")
    return RedirectResponse(request.url.path, status_code=303)


def require_login(request: Request) -> None:
    if not request.session.get("auth"):
        raise LoginRequired


async def read_form(request: Request):
    """Read a POSTed form and check its CSRF token."""
    form = await request.form()
    expected = request.session.get("csrf", "")
    if not expected or not hmac.compare_digest(form.get("csrf", ""), expected):
        raise InvalidCSRF
    return form


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flashes", []).append([kind, message])


def render(request: Request, template: str, status_code: int = 200, **context):
    return templates.TemplateResponse(
        request,
        template,
        {
            "restaurant_name": load_config()["restaurant"]["name"],
            "authenticated": bool(request.session.get("auth")),
            "csrf": request.session.get("csrf", ""),
            "flashes": request.session.pop("flashes", []),
            **context,
        },
        status_code=status_code,
    )


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #


@app.get("/login")
async def login_page(request: Request):
    if request.session.get("auth"):
        return redirect("/")
    return render(request, "login.html")


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    password = form.get("password", "")
    if PASSWORD and hmac.compare_digest(password.encode(), PASSWORD.encode()):
        request.session.clear()
        request.session["auth"] = True
        request.session["csrf"] = secrets.token_urlsafe(32)
        return redirect("/")
    await asyncio.sleep(1)  # slow down password guessing
    return render(request, "login.html", status_code=401, error="Password errata.")


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return redirect("/login")


# --------------------------------------------------------------------------- #
# Protected pages
# --------------------------------------------------------------------------- #

router = APIRouter(dependencies=[Depends(require_login)])


def summarize(bookings: list[dict]) -> dict:
    confirmed = [b for b in bookings if b["status"] == "confirmed"]
    return {"count": len(confirmed), "covers": sum(b["people"] for b in confirmed)}


def rows(start: date, end: date) -> list[dict]:
    return [dict(r) for r in db.get_bookings_between(start, end, include_cancelled=True)]


@router.get("/")
async def bookings_page(request: Request, day: str | None = None):
    today = now().date()
    closed = load_config()["restaurant"].get("closed_weekdays", [])
    context = {"today": today, "today_iso": today.isoformat(), "closed_weekdays": closed}

    if day:
        try:
            selected = date.fromisoformat(day)
        except ValueError:
            flash(request, "Data non valida.", "error")
            return redirect("/")
        bookings = rows(selected, selected)
        return render(
            request, "bookings.html", nav="bookings", selected=selected,
            bookings=bookings, stats=summarize(bookings), **context,
        )

    today_bookings = rows(today, today)
    upcoming = rows(today + timedelta(days=1), today + timedelta(days=UPCOMING_DAYS))
    days = []
    for offset in range(1, UPCOMING_DAYS + 1):
        d = today + timedelta(days=offset)
        day_bookings = [b for b in upcoming if b["date"] == d.isoformat()]
        days.append({"date": d, "bookings": day_bookings, "stats": summarize(day_bookings)})
    return render(
        request, "bookings.html", nav="bookings", selected=None,
        bookings=today_bookings, stats=summarize(today_bookings), days=days,
        week_stats=summarize(upcoming), **context,
    )


# --- Cancel a booking ---


async def notify_customer(user_id: int, text: str) -> bool:
    """Send a Telegram message to a customer through the bot. Returns True on success."""
    if not TOKEN:
        return False
    try:
        async with Bot(TOKEN) as bot:
            await bot.send_message(user_id, text, parse_mode=ParseMode.HTML)
        return True
    except TelegramError:
        # e.g. the customer blocked the bot, or the token is wrong.
        logger.exception("Could not notify customer %s", user_id)
        return False


def get_booking_or_none(booking_id: int) -> dict | None:
    booking = db.get_booking(booking_id)
    return dict(booking) if booking else None


def safe_next(url: str | None) -> str:
    """Only allow redirects to pages of this site."""
    return url if url and url.startswith("/") and not url.startswith("//") else "/"


@router.get("/bookings/{booking_id}/cancel")
async def cancel_page(request: Request, booking_id: int, next: str | None = None):
    booking = get_booking_or_none(booking_id)
    if not booking or booking["status"] != "confirmed":
        flash(request, "Prenotazione non trovata o già cancellata.", "error")
        return redirect(safe_next(next))
    return render(request, "cancel.html", nav="bookings", booking=booking, next=safe_next(next))


@router.post("/bookings/{booking_id}/cancel")
async def cancel_booking(request: Request, booking_id: int):
    form = await read_form(request)
    back = safe_next(form.get("next"))
    booking = get_booking_or_none(booking_id)
    if not booking or not db.cancel_booking_by_owner(booking_id):
        flash(request, "Prenotazione non trovata o già cancellata.", "error")
        return redirect(back)
    logger.info("Booking #%s cancelled from the dashboard", booking_id)

    when = f"{format_date(date.fromisoformat(booking['date']))} alle {booking['time']}"
    if booking["source"] == "manual" or booking["user_id"] == db.MANUAL_USER_ID:
        contact = f" ({booking['phone']})" if booking["phone"] else ""
        flash(request, f"Prenotazione cancellata. Ricordati di avvisare {booking['name']}{contact}.", "warn")
    else:
        info = load_config()["restaurant"]
        phone = f"\nPer informazioni puoi chiamarci al {escape(info['phone'])}." if info.get("phone") else ""
        sent = await notify_customer(
            booking["user_id"],
            f"😔 Ci dispiace, la tua prenotazione <b>n. {booking_id}</b> per {when} "
            f"({booking['people']} persone) è stata cancellata dal ristorante.{phone}",
        )
        if sent:
            flash(request, "Prenotazione cancellata. Il cliente è stato avvisato su Telegram.")
        else:
            flash(
                request,
                "Prenotazione cancellata, ma non è stato possibile avvisare il cliente su Telegram: "
                "contattalo tu, se possibile.",
                "warn",
            )
    return redirect(back)


# --- Manual booking ---


def booking_form_context(values: dict | None = None, errors: list[str] | None = None) -> dict:
    restaurant = load_config()["restaurant"]
    return {
        "nav": "new",
        "values": values or {"date": now().date().isoformat(), "people": "2"},
        "errors": errors or [],
        "slots": restaurant["booking_times"],
    }


@router.get("/bookings/new")
async def new_booking_page(request: Request):
    return render(request, "booking_new.html", **booking_form_context())


@router.post("/bookings/new")
async def new_booking(request: Request):
    form = await read_form(request)
    values = {k: (form.get(k) or "").strip() for k in ("name", "phone", "people", "date", "time", "notes")}
    errors = []

    if not 2 <= len(values["name"]) <= 50:
        errors.append("Inserisci un nome tra 2 e 50 caratteri.")
    people = int(values["people"]) if values["people"].isdigit() else 0
    if not 1 <= people <= MAX_MANUAL_PEOPLE:
        errors.append(f"Il numero di persone deve essere tra 1 e {MAX_MANUAL_PEOPLE}.")
    try:
        booking_date = date.fromisoformat(values["date"])
        if booking_date < now().date():
            errors.append("La data è già passata.")
    except ValueError:
        booking_date = None
        errors.append("Inserisci una data valida.")
    booking_time = normalize_time(values["time"])
    if booking_time is None:
        errors.append("Inserisci un orario valido, es. 20:30.")
    if len(values["notes"]) > 300:
        errors.append("Le note possono avere al massimo 300 caratteri.")

    if errors:
        return render(request, "booking_new.html", status_code=400, **booking_form_context(values, errors))

    booking_id = db.add_booking(
        user_id=db.MANUAL_USER_ID,
        username=None,
        name=values["name"],
        people=people,
        booking_date=booking_date,
        booking_time=booking_time,
        notes=values["notes"] or None,
        phone=values["phone"] or None,
        source="manual",
    )
    logger.info("Manual booking #%s added from the dashboard", booking_id)
    message = f"Prenotazione n. {booking_id} aggiunta: {values['name']}, {people} persone, {format_date(booking_date)} alle {booking_time}."
    if booking_date.weekday() in load_config()["restaurant"].get("closed_weekdays", []):
        flash(request, message + " Attenzione: è un giorno di chiusura.", "warn")
    else:
        flash(request, message)
    return redirect(f"/?day={booking_date.isoformat()}")


# --- Menu and opening hours ---


@router.get("/menu")
async def menu_page(request: Request):
    return render(request, "menu.html", nav="menu", form=menu_editor.form_view(load_config()), errors=[])


@router.post("/menu")
async def save_menu(request: Request):
    form = await read_form(request)
    config, errors = menu_editor.parse_form(form)
    if errors:
        return render(
            request, "menu.html", status_code=400, nav="menu",
            form=menu_editor.form_view(config), errors=errors,
        )
    menu_editor.save_config(config)
    logger.info("menu.json updated from the dashboard")
    flash(request, "Modifiche salvate. Il bot le usa già, non serve riavviarlo.")
    return redirect("/menu")


app.include_router(router)
