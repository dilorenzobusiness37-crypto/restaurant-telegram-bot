"""Owner dashboard: bookings overview, manual bookings, menu editor and settings."""

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import secrets
import zlib
from datetime import date, timedelta
from html import escape
from pathlib import Path

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import UploadFile
from starlette.middleware.sessions import SessionMiddleware
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

import database as db  # noqa: E402
from bot import format_date, load_config, normalize_time, now  # noqa: E402

from . import menu_editor, settings_store as store  # noqa: E402

logger = logging.getLogger("restaurant-dashboard")

PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()

UPCOMING_DAYS = 7
SESSION_MAX_AGE = 7 * 24 * 3600  # stay logged in for a week
MAX_MANUAL_PEOPLE = 200
NEXT_ARRIVALS = 3

HERE = Path(__file__).parent
mimetypes.add_type("font/woff2", ".woff2")  # not known to Python's mimetypes on every system

app = FastAPI(title="Restaurant dashboard", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=store.session_secret(),
    session_cookie="dashboard_session",
    max_age=SESSION_MAX_AGE,
    same_site="lax",
)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


# --------------------------------------------------------------------------- #
# Presentation helpers (used by the templates)
# --------------------------------------------------------------------------- #


def to_minutes(hm: str) -> int:
    hours, minutes = hm.split(":")
    return int(hours) * 60 + int(minutes)


# Words in the booking notes that deserve a visible flag during service.
NOTE_TAGS = {
    "Allergie": ("allerg", "celiac", "glutine", "lattosio", "intoller", "noci", "arachidi", "crostacei", "vegan"),
    "Bambini": ("bambin", "seggiolon", "passeggino", "neonat", "bimb"),
    "Festa": ("compleann", "anniversari", "festa", "laurea", "torta"),
}


def note_tags(notes: str | None) -> list[str]:
    text = (notes or "").lower()
    return [tag for tag, words in NOTE_TAGS.items() if any(w in text for w in words)]


AVATAR_TONES = 6
_NAME_FILLERS = {"e", "di", "de", "da", "del", "della", "la", "il", "sig", "sigra", "signor", "signora", "famiglia", "fam"}


def avatar(name: str) -> dict:
    """Initials plus a colour tone that is always the same for the same name."""
    words = [w for w in re.split(r"[\s.'-]+", name) if w and w[0].isalpha()]
    meaningful = [w for w in words if w.lower() not in _NAME_FILLERS] or words
    initials = "".join(w[0] for w in meaningful[:2]).upper() or "?"
    return {"initials": initials, "tone": zlib.crc32(name.strip().lower().encode()) % AVATAR_TONES}


def minutes_until(hm: str) -> int:
    current = now()
    return to_minutes(hm) - (current.hour * 60 + current.minute)


def service_status() -> dict | None:
    """'Servizio serale in corso', 'Sala chiusa oggi', ... from opening days and time slots."""
    restaurant = load_config()["restaurant"]
    current = now()
    if current.weekday() in restaurant.get("closed_weekdays", []):
        return {"label": "Sala chiusa oggi", "kind": "closed"}
    times = sorted(restaurant.get("booking_times") or [])
    if not times:
        return None
    first, last = to_minutes(times[0]), to_minutes(times[-1])
    turn = restaurant.get("table_turn_minutes") or menu_editor.DEFAULT_TURN_MINUTES
    meal = "serale" if first >= 17 * 60 else "di pranzo"
    minute = current.hour * 60 + current.minute
    if minute < first - 30:
        return {"label": f"Servizio {meal} dalle {times[0]}", "kind": "before"}
    if minute <= last + turn:
        return {"label": f"Servizio {meal} in corso", "kind": "live"}
    return {"label": "Servizio concluso", "kind": "after"}


def asset_version() -> str:
    """Changes whenever a file in static/ changes: appended to CSS/JS URLs so browsers
    never keep an outdated script (e.g. one without live updates) in their cache."""
    latest = max(p.stat().st_mtime_ns for p in (HERE / "static").rglob("*") if p.is_file())
    return format(latest, "x")[-10:]


def logo_url() -> str | None:
    path = store.logo_path()
    return f"/logo?v={int(path.stat().st_mtime)}" if path else None


templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(
    format_date=format_date,
    now_hm=lambda: now().strftime("%H:%M"),
    day_iso=lambda offset=0: (now().date() + timedelta(days=offset)).isoformat(),
    booking_slots=lambda: load_config()["restaurant"]["booking_times"],
    asset_version=asset_version,
)
templates.env.filters.update(
    todate=date.fromisoformat,
    note_tags=note_tags,
    avatar=avatar,
    minutes_until=minutes_until,
    price_input=lambda p: f"{p:.2f}".replace(".", ",") if isinstance(p, (int, float)) else (p or ""),
)


# --------------------------------------------------------------------------- #
# Session helpers
# --------------------------------------------------------------------------- #


class LoginRequired(Exception):
    pass


class InvalidCSRF(Exception):
    pass


def wants_json(request: Request) -> bool:
    """Requests sent by the page's own JavaScript (e.g. cancel with undo)."""
    return request.headers.get("x-requested-with") == "fetch"


@app.exception_handler(LoginRequired)
async def redirect_to_login(request: Request, exc: LoginRequired):
    if wants_json(request):
        return JSONResponse({"ok": False, "message": "Sessione scaduta: accedi di nuovo."}, status_code=401)
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(InvalidCSRF)
async def invalid_csrf(request: Request, exc: InvalidCSRF):
    if wants_json(request):
        return JSONResponse({"ok": False, "message": "Sessione scaduta: ricarica la pagina."}, status_code=403)
    flash(request, "Sessione scaduta: riprova.", "error")
    return RedirectResponse(request.url.path, status_code=303)


def require_login(request: Request) -> None:
    # The fingerprint changes with the password, so changing it logs out other sessions.
    if not request.session.get("auth") or request.session.get("pw") != store.credential_fingerprint(PASSWORD):
        request.session.clear()
        raise LoginRequired


async def read_form(request: Request):
    """Read a POSTed form and check its CSRF token."""
    form = await request.form()
    expected = request.session.get("csrf", "")
    if not expected or not secrets.compare_digest(str(form.get("csrf", "")), expected):
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
            "highlight_id": request.session.pop("highlight", None),
            "logo_url": logo_url(),
            "status": service_status(),
            "today_long": format_date(now().date()).capitalize(),
            **context,
        },
        status_code=status_code,
    )


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# --------------------------------------------------------------------------- #
# Login and public assets
# --------------------------------------------------------------------------- #


@app.get("/login")
async def login_page(request: Request):
    if request.session.get("auth") and request.session.get("pw") == store.credential_fingerprint(PASSWORD):
        return redirect("/")
    return render(request, "login.html")


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    if store.check_password(str(form.get("password", "")), PASSWORD):
        request.session.clear()
        request.session["auth"] = True
        request.session["pw"] = store.credential_fingerprint(PASSWORD)
        request.session["csrf"] = secrets.token_urlsafe(32)
        return redirect("/")
    await asyncio.sleep(1)  # slow down password guessing
    return render(request, "login.html", status_code=401, error="Password errata.")


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return redirect("/login")


@app.get("/logo")
async def logo():
    """The restaurant logo: public, it is also shown on the login page."""
    path = store.logo_path()
    if not path:
        return Response(status_code=404)
    return FileResponse(path, headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"})


# --------------------------------------------------------------------------- #
# Bookings
# --------------------------------------------------------------------------- #

router = APIRouter(dependencies=[Depends(require_login)])


def summarize(bookings: list[dict]) -> dict:
    confirmed = [b for b in bookings if b["status"] == "confirmed"]
    return {"count": len(confirmed), "covers": sum(b["people"] for b in confirmed)}


def rows(start: date, end: date) -> list[dict]:
    return [dict(r) for r in db.get_bookings_between(start, end, include_cancelled=True)]


def room_load(confirmed: list[dict], restaurant: dict, live: bool) -> dict:
    """People seated in each time slot, assuming every table stays for `table_turn_minutes`.

    One booking counts as one table: it is an estimate, and the page says so.
    """
    capacity = restaurant.get("capacity") if isinstance(restaurant.get("capacity"), int) else None
    tables = restaurant.get("tables") if isinstance(restaurant.get("tables"), int) else None
    turn = restaurant.get("table_turn_minutes") or menu_editor.DEFAULT_TURN_MINUTES
    times = sorted(set(restaurant.get("booking_times", [])) | {b["time"] for b in confirmed})
    current = now().strftime("%H:%M")

    slots = []
    for i, t in enumerate(times):
        start = to_minutes(t)
        seated = [b for b in confirmed if to_minutes(b["time"]) <= start < to_minutes(b["time"]) + turn]
        covers = sum(b["people"] for b in seated)
        next_start = times[i + 1] if i + 1 < len(times) else "24:00"
        slots.append({
            "time": t,
            "covers": covers,
            "arriving": sum(b["people"] for b in confirmed if b["time"] == t),
            "tables_free": max(0, tables - len(seated)) if tables else None,
            "full": bool(capacity) and covers >= capacity,
            "is_now": live and t <= current < next_start,
        })
    peak = max(slots, key=lambda s: s["covers"], default=None)
    scale = capacity or (peak["covers"] if peak and peak["covers"] else 1)
    for s in slots:
        s["pct"] = min(100, round(s["covers"] / scale * 100))
    return {
        "slots": slots,
        "capacity": capacity,
        "tables": tables,
        "turn_label": (f"{turn // 60} {'ora' if turn == 60 else 'ore'}" if turn % 60 == 0 else f"{turn} minuti"),
        "peak": peak if peak and peak["covers"] else None,
        "peak_pct": round(peak["covers"] / capacity * 100) if capacity and peak else None,
    }


def notes_summary(confirmed: list[dict]) -> list[str]:
    """'3 con allergie', '2 con bambini'... for the service band."""
    counts = {tag: 0 for tag in NOTE_TAGS}
    for b in confirmed:
        for tag in note_tags(b["notes"]):
            counts[tag] += 1
    labels = []
    if counts["Allergie"]:
        labels.append(("allergie", f"{counts['Allergie']} con allergie"))
    if counts["Bambini"]:
        labels.append(("bambini", f"{counts['Bambini']} con bambini"))
    if counts["Festa"]:
        n = counts["Festa"]
        labels.append(("festa", f"{n} {'occasione speciale' if n == 1 else 'occasioni speciali'}"))
    phone = sum(1 for b in confirmed if b["source"] == "manual")
    if phone:
        labels.append(("telefono", f"{phone} al telefono"))
    return labels


# What a booking contributes to the page: if any of these change, the page must be redrawn.
STATE_FIELDS = ("id", "date", "time", "name", "people", "notes", "status", "cancelled_by", "phone", "source")


def board_state(selected: date | None) -> dict:
    """Bookings shown on the home page (today and the next days, or one chosen day)
    plus a short version string that changes whenever the page would look different.
    """
    today = now().date()
    start, end = (selected, selected) if selected else (today, today + timedelta(days=UPCOMING_DAYS))
    bookings = rows(start, end)
    live = (selected or today) == today
    fingerprint = json.dumps(
        [
            [[b[k] for k in STATE_FIELDS] for b in bookings],
            load_config()["restaurant"],  # capacity, time slots, closed days, name
            # Today the page shows the time ("Adesso", countdowns): redraw once a minute.
            now().strftime("%Y-%m-%d %H:%M") if live else today.isoformat(),
        ],
        sort_keys=True, default=str,
    )
    return {
        "version": hashlib.sha1(fingerprint.encode()).hexdigest()[:16],
        "bookings": [
            {k: b[k] for k in ("id", "date", "time", "name", "people", "status", "cancelled_by")}
            for b in bookings
        ],
    }


def parse_day(day: str | None) -> date | None:
    """ISO date from the query string; raises ValueError if it is not one."""
    return date.fromisoformat(day) if day else None


@router.get("/api/bookings/state")
async def bookings_state(day: str | None = None):
    """Polled every few seconds by the home page to notice bookings made or cancelled in the bot."""
    try:
        state = board_state(parse_day(day))
    except ValueError:
        return JSONResponse({"ok": False, "message": "Data non valida."}, status_code=400)
    return JSONResponse(state, headers={"Cache-Control": "no-store"})


@router.get("/")
async def bookings_page(request: Request, day: str | None = None):
    today = now().date()
    restaurant = load_config()["restaurant"]
    context = {"today": today, "today_iso": today.isoformat(), "closed_weekdays": restaurant.get("closed_weekdays", [])}

    try:
        selected = parse_day(day)
    except ValueError:
        flash(request, "Data non valida.", "error")
        return redirect("/")

    shown_day = selected or today
    live = shown_day == today
    bookings = rows(shown_day, shown_day)
    confirmed = [b for b in bookings if b["status"] == "confirmed"]
    current = now().strftime("%H:%M")
    context.update(
        selected=selected,
        bookings=bookings,
        stats=summarize(bookings),
        room=room_load(confirmed, restaurant, live),
        summary=notes_summary(confirmed),
        next_arrivals=[b for b in confirmed if b["time"] >= current][:NEXT_ARRIVALS] if live else [],
    )

    if not selected:
        upcoming = rows(today + timedelta(days=1), today + timedelta(days=UPCOMING_DAYS))
        days = []
        for offset in range(1, UPCOMING_DAYS + 1):
            d = today + timedelta(days=offset)
            day_bookings = [b for b in upcoming if b["date"] == d.isoformat()]
            days.append({"date": d, "bookings": day_bookings, "stats": summarize(day_bookings)})
        context.update(days=days, week_stats=summarize(upcoming))

    context["board_version"] = board_state(selected)["version"]
    return render(request, "bookings.html", nav="bookings", **context)


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
    """Confirmation page, used when JavaScript is off (otherwise the list offers 'Annulla')."""
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

    def respond(kind: str, message: str, ok: bool = True, status: int = 200):
        if wants_json(request):
            return JSONResponse({"ok": ok, "kind": kind, "message": message}, status_code=status)
        flash(request, message, kind)
        return redirect(back)

    if not booking or not db.cancel_booking_by_owner(booking_id):
        return respond("error", "Prenotazione non trovata o già cancellata.", ok=False, status=409)
    logger.info("Booking #%s cancelled from the dashboard", booking_id)

    when = f"{format_date(date.fromisoformat(booking['date']))} alle {booking['time']}"
    if booking["source"] == "manual" or booking["user_id"] == db.MANUAL_USER_ID:
        contact = f" ({booking['phone']})" if booking["phone"] else ""
        return respond("warn", f"Prenotazione cancellata. Ricordati di avvisare {booking['name']}{contact}.")

    info = load_config()["restaurant"]
    phone = f"\nPer informazioni puoi chiamarci al {escape(info['phone'])}." if info.get("phone") else ""
    sent = await notify_customer(
        booking["user_id"],
        f"😔 Ci dispiace, la tua prenotazione <b>n. {booking_id}</b> per {when} "
        f"({booking['people']} persone) è stata cancellata dal ristorante.{phone}",
    )
    if sent:
        return respond("ok", f"Prenotazione di {booking['name']} cancellata. Il cliente è stato avvisato su Telegram.")
    return respond(
        "warn",
        f"Prenotazione di {booking['name']} cancellata, ma non è stato possibile avvisare il cliente "
        "su Telegram: contattalo tu, se possibile.",
    )


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
    request.session["highlight"] = booking_id  # the new row flashes once in the list
    return redirect(f"/?day={booking_date.isoformat()}" if booking_date != now().date() else "/")


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
    flash(request, "Menù salvato. Il bot lo usa già, non serve riavviarlo.")
    return redirect("/menu")


# --- Settings ---


def settings_context(form: dict, errors: list[str] | None = None, password_errors: list[str] | None = None) -> dict:
    return {
        "nav": "settings",
        "form": form,
        "errors": errors or [],
        "password_errors": password_errors or [],
        "custom_password": store.has_custom_password(),
        "max_logo_label": f"{store.MAX_LOGO_BYTES // (1024 * 1024)} MB",
    }


@router.get("/settings")
async def settings_page(request: Request):
    return render(request, "settings.html", **settings_context(menu_editor.form_view(load_config())))


async def read_logo(upload) -> tuple[bytes | None, str | None, str | None]:
    """Returns (content, extension, error)."""
    if not isinstance(upload, UploadFile) or not upload.filename:
        return None, None, None
    content = await upload.read(store.MAX_LOGO_BYTES + 1)
    if len(content) > store.MAX_LOGO_BYTES:
        return None, None, f"Il logo è troppo pesante: massimo {store.MAX_LOGO_BYTES // (1024 * 1024)} MB."
    ext = store.detect_logo_type(content)
    if ext is None:
        return None, None, "Il logo deve essere un'immagine PNG, JPG o WebP."
    return content, ext, None


@router.post("/settings")
async def save_settings(request: Request):
    form = await read_form(request)
    config, errors = menu_editor.parse_settings_form(form)
    content, ext, logo_error = await read_logo(form.get("logo"))
    if logo_error:
        errors.append(logo_error)
    if errors:
        return render(request, "settings.html", status_code=400, **settings_context(menu_editor.form_view(config), errors))

    menu_editor.save_config(config)
    if content:
        store.save_logo(content, ext)
    elif form.get("remove_logo"):
        store.remove_logo()
    logger.info("Settings updated from the dashboard")
    flash(request, "Impostazioni salvate.")
    return redirect("/settings")


@router.post("/settings/password")
async def change_password(request: Request):
    form = await read_form(request)
    current, new, confirm = (str(form.get(k, "")) for k in ("current", "new", "confirm"))
    errors = []
    if not store.check_password(current, PASSWORD):
        errors.append("La password attuale non è corretta.")
    if len(new) < store.MIN_PASSWORD_LENGTH:
        errors.append(f"La nuova password deve avere almeno {store.MIN_PASSWORD_LENGTH} caratteri.")
    elif new != confirm:
        errors.append("Le due nuove password non coincidono.")
    if errors:
        if errors[0].startswith("La password attuale"):
            await asyncio.sleep(1)
        return render(
            request, "settings.html", status_code=400,
            **settings_context(menu_editor.form_view(load_config()), password_errors=errors),
        )
    store.set_password(new)
    # Keep this session, every other one is logged out by the new fingerprint.
    request.session["pw"] = store.credential_fingerprint(PASSWORD)
    logger.info("Dashboard password changed")
    flash(request, "Password cambiata. Le altre sessioni aperte sono state chiuse.")
    return redirect("/settings")


app.include_router(router)
