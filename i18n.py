"""Translations shared by the bot and the dashboard.

Texts live in locales/<language>.json. Keys are dotted paths ("bot.btn.menu").
Missing keys fall back to the default language, then to Italian.
"""

import json
import os
import re
from datetime import date
from functools import lru_cache
from pathlib import Path

LOCALES_DIR = Path(__file__).parent / "locales"

# Languages the bot speaks to customers, and the ones the dashboard offers.
SUPPORTED = ("it", "en", "fr", "de", "es")
PANEL_LANGUAGES = ("it", "en")
FALLBACK = "it"


def _env_default() -> str:
    lang = os.getenv("DEFAULT_LANGUAGE", FALLBACK).strip().lower()
    return lang if lang in SUPPORTED else FALLBACK


@lru_cache(maxsize=None)
def catalog(lang: str) -> dict:
    with open(LOCALES_DIR / f"{lang}.json", encoding="utf-8") as f:
        return json.load(f)


def default_language() -> str:
    """Language used when the customer's phone language is not supported."""
    return _env_default()


def normalize(code: str | None) -> str | None:
    """'de-AT' -> 'de'; None if the language is not supported."""
    if not code:
        return None
    lang = code.split("-")[0].split("_")[0].lower()
    return lang if lang in SUPPORTED else None


def panel_language(config: dict) -> str:
    """Language of the dashboard and of the owner's notifications (set in Impostazioni)."""
    lang = config.get("restaurant", {}).get("panel_language")
    if lang in PANEL_LANGUAGES:
        return lang
    default = default_language()
    return default if default in PANEL_LANGUAGES else "en"


def _lookup(lang: str, key: str):
    node = catalog(lang)
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def raw(lang: str, key: str):
    """The value for key (string, list or dict), with language fallbacks."""
    for candidate in (lang, default_language(), FALLBACK):
        if candidate in SUPPORTED:
            value = _lookup(candidate, key)
            if value is not None:
                return value
    raise KeyError(f"Missing translation: {key}")


def t(lang: str, key: str, **params) -> str:
    text = raw(lang, key)
    return text.format(**params) if params else text


def tn(lang: str, key: str, n: int, **params) -> str:
    """Plural form: the key holds {"one": ..., "other": ...}; {n} is available."""
    forms = raw(lang, key)
    return forms["one" if n == 1 else "other"].format(n=n, **params)


def all_labels(key: str) -> set[str]:
    """The text of key in every language: used to recognise buttons whatever the language."""
    return {_lookup(lang, key) for lang in SUPPORTED if isinstance(_lookup(lang, key), str)}


def labels_regex(key: str) -> str:
    return "^(" + "|".join(re.escape(label) for label in sorted(all_labels(key))) + ")$"


# --- Dates and prices ---


def ucfirst(text: str) -> str:
    return text[:1].upper() + text[1:]


def weekday_name(d: date | int, lang: str) -> str:
    index = d if isinstance(d, int) else d.weekday()
    return raw(lang, "common.weekdays")[index]


def format_date(d: date, lang: str) -> str:
    """Written out in full: 'venerdì 2 ottobre 2026', 'Friday, October 2, 2026'..."""
    return t(lang, "common.date_long", weekday=weekday_name(d, lang), day=d.day,
             month=raw(lang, "common.months")[d.month - 1], year=d.year)


def format_day_month(d: date, lang: str) -> str:
    """'2 ottobre', 'October 2', '2. Oktober'..."""
    return t(lang, "common.date_day_month", day=d.day, month=raw(lang, "common.months")[d.month - 1])


def format_amount(amount: float, lang: str) -> str:
    return f"{amount:.2f}".replace(".", t(lang, "common.decimal"))


def format_price(amount: float, lang: str) -> str:
    """'€ 12,00' (it), '€12.00' (en), '12,00 €' (fr, de, es)."""
    return t(lang, "common.price", amount=format_amount(amount, lang))


def note_keywords() -> dict[str, set[str]]:
    """Words that flag a note (allergies, children, celebrations), gathered from every language."""
    words: dict[str, set[str]] = {}
    for lang in SUPPORTED:
        for tag, items in (_lookup(lang, "common.note_keywords") or {}).items():
            words.setdefault(tag, set()).update(w.lower() for w in items)
    return words
