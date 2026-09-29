"""Read the "Menù e orari" form, validate it and save it to menu.json.

The bot re-reads menu.json on every request, so a saved change is live immediately.
"""

import copy
import json
import os
import re
import shutil

from bot import MENU_PATH, WEEKDAYS_IT, load_config, normalize_time

BACKUP_PATH = MENU_PATH.with_suffix(".json.bak")

# Empty rows shown in the form to add new dishes / a new category.
BLANK_ITEMS_PER_CATEGORY = 2
BLANK_ITEMS_NEW_CATEGORY = 3

MAX_PRICE = 10_000
MAX_PEOPLE_LIMIT = 500


def parse_price(raw: str) -> float | None:
    """'12', '12.5', '12,50', '€ 12,50' -> 12.5; None if not a valid price."""
    cleaned = raw.replace("€", "").replace(" ", "").replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d{1,2})?", cleaned):
        return None
    return float(cleaned)


def form_view(config: dict) -> dict:
    """Turn a config into what the form template needs, including empty rows to fill in."""
    restaurant = config["restaurant"]
    categories = []
    for section in config["menu"]:
        items = [dict(item) for item in section["items"]]
        items += [{} for _ in range(BLANK_ITEMS_PER_CATEGORY)]
        categories.append({"category": section["category"], "emoji": section.get("emoji", ""), "items": items})
    categories.append(
        {"category": "", "emoji": "", "items": [{} for _ in range(BLANK_ITEMS_NEW_CATEGORY)], "new": True}
    )
    return {
        "name": restaurant.get("name", ""),
        "address": restaurant.get("address", ""),
        "phone": restaurant.get("phone", ""),
        "maps_url": restaurant.get("maps_url", ""),
        "opening_hours": "\n".join(restaurant.get("opening_hours", [])),
        "closed_weekdays": restaurant.get("closed_weekdays", []),
        "booking_times": ", ".join(restaurant.get("booking_times", [])),
        "max_people": restaurant.get("max_people", 20),
        "categories": categories,
        "weekdays": list(enumerate(WEEKDAYS_IT)),
    }


def parse_form(form) -> tuple[dict, list[str]]:
    """Build a new config from the submitted form.

    Returns (config, errors). If errors is not empty the config must not be saved,
    but it still holds what the user typed so the form can be shown again.
    """
    errors: list[str] = []
    text = lambda key: (form.get(key) or "").strip()  # noqa: E731

    # Start from the current file so any extra keys the form doesn't know about are kept.
    config = copy.deepcopy(load_config())
    restaurant = config.setdefault("restaurant", {})

    # --- Restaurant info ---
    restaurant["name"] = text("name")
    restaurant["address"] = text("address")
    restaurant["phone"] = text("phone")
    restaurant["maps_url"] = text("maps_url")
    if not restaurant["name"]:
        errors.append("Il nome del ristorante è obbligatorio.")
    if not restaurant["address"]:
        errors.append("L'indirizzo è obbligatorio.")
    if restaurant["maps_url"] and not restaurant["maps_url"].startswith(("http://", "https://")):
        errors.append("Il link a Google Maps deve iniziare con https://")

    restaurant["opening_hours"] = [line.strip() for line in text("opening_hours").splitlines() if line.strip()]
    if not restaurant["opening_hours"]:
        errors.append("Inserisci almeno una riga negli orari di apertura.")

    closed = sorted({int(d) for d in form.getlist("closed_weekdays") if d.isdigit() and 0 <= int(d) <= 6})
    restaurant["closed_weekdays"] = closed
    if len(closed) == 7:
        errors.append("Non puoi segnare tutti i giorni come chiusi.")

    raw_times = [t for t in re.split(r"[,\s;]+", text("booking_times")) if t]
    times = []
    for raw in raw_times:
        normalized = normalize_time(raw)
        if normalized is None:
            errors.append(f"Fascia oraria non valida: «{raw}». Usa il formato HH:MM, es. 20:30.")
        elif normalized not in times:
            times.append(normalized)
    restaurant["booking_times"] = sorted(times)
    if not raw_times:
        errors.append("Inserisci almeno una fascia oraria prenotabile.")

    max_people = text("max_people")
    if max_people.isdigit() and 1 <= int(max_people) <= MAX_PEOPLE_LIMIT:
        restaurant["max_people"] = int(max_people)
    else:
        restaurant["max_people"] = max_people
        errors.append(f"Il numero massimo di persone deve essere tra 1 e {MAX_PEOPLE_LIMIT}.")

    # --- Menu ---
    menu = []
    cat_count = int(form.get("cat_count") or 0)
    for i in range(cat_count):
        p = f"cat{i}_"
        if form.get(p + "delete"):
            continue
        name, emoji = text(p + "name"), text(p + "emoji")
        items = []
        for j in range(int(form.get(p + "item_count") or 0)):
            q = f"{p}item{j}_"
            if form.get(q + "delete"):
                continue
            item_name, desc, raw_price = text(q + "name"), text(q + "desc"), text(q + "price")
            if not (item_name or desc or raw_price):
                continue  # empty row
            price = parse_price(raw_price)
            label = f"«{item_name}»" if item_name else f"senza nome in «{name or 'nuova categoria'}»"
            if not item_name:
                errors.append(f"C'è un piatto {label}: inserisci il nome.")
            if price is None:
                errors.append(f"Prezzo non valido per il piatto {label}: usa un numero, es. 12,50.")
            elif price > MAX_PRICE:
                errors.append(f"Prezzo troppo alto per il piatto {label}.")
            items.append({"name": item_name, "description": desc, "price": price if price is not None else raw_price})

        if not (name or emoji or items):
            continue  # untouched "new category" block
        if not name:
            errors.append("Una categoria ha dei piatti ma nessun nome.")
        elif not items:
            errors.append(f"La categoria «{name}» non ha piatti: aggiungine almeno uno o eliminala.")
        menu.append({"category": name, "emoji": emoji, "items": items})

    if not menu:
        errors.append("Il menù deve avere almeno una categoria.")
    config["menu"] = menu
    return config, errors


def save_config(config: dict) -> None:
    """Write menu.json atomically, keeping the previous version as menu.json.bak."""
    tmp_path = MENU_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
        f.write("\n")
    if MENU_PATH.exists():
        shutil.copyfile(MENU_PATH, BACKUP_PATH)
    # os.replace is atomic: the bot never reads a half-written file.
    os.replace(tmp_path, MENU_PATH)
