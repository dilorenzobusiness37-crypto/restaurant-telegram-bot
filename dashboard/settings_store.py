"""Dashboard-only settings kept outside menu.json: password hash, session key, logo.

Stored in data/ (git-ignored). The restaurant name and room capacity live in
menu.json instead, because the bot reads them too.
"""

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

DATA_DIR = Path(os.getenv("DASHBOARD_DATA_DIR") or Path(__file__).resolve().parent.parent / "data")
SETTINGS_PATH = DATA_DIR / "dashboard_settings.json"

MAX_LOGO_BYTES = 1024 * 1024
# Raster formats only: an SVG could carry scripts.
LOGO_TYPES = {"png": b"\x89PNG\r\n\x1a\n", "jpg": b"\xff\xd8\xff"}
PBKDF2_ITERATIONS = 240_000
MIN_PASSWORD_LENGTH = 8


def _load() -> dict:
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save(settings: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp, SETTINGS_PATH)


# --- Session key ---


def session_secret() -> str:
    """Random key that signs the session cookie, created on first run."""
    settings = _load()
    if not settings.get("session_secret"):
        settings["session_secret"] = secrets.token_hex(32)
        _save(settings)
    return settings["session_secret"]


# --- Password ---


def _hash(password: str, salt: bytes) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def has_custom_password() -> bool:
    return bool(_load().get("password_hash"))


def check_password(password: str, env_password: str) -> bool:
    """A password set from the dashboard replaces the one in .env."""
    stored = _load().get("password_hash")
    if stored:
        _, iterations, salt, digest = stored.split("$")
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(candidate.hex(), digest)
    return bool(env_password) and hmac.compare_digest(password.encode(), env_password.encode())


def set_password(password: str) -> None:
    settings = _load()
    settings["password_hash"] = _hash(password, secrets.token_bytes(16))
    _save(settings)


def credential_fingerprint(env_password: str) -> str:
    """Changes whenever the password changes: stored in the session to log out old sessions."""
    source = _load().get("password_hash") or f"env:{env_password}"
    return hashlib.sha256(source.encode()).hexdigest()[:16]


# --- Logo ---


def detect_logo_type(content: bytes) -> str | None:
    for ext, magic in LOGO_TYPES.items():
        if content.startswith(magic):
            return ext
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp"
    return None


def logo_path() -> Path | None:
    for ext in ("png", "jpg", "webp"):
        path = DATA_DIR / f"logo.{ext}"
        if path.exists():
            return path
    return None


def save_logo(content: bytes, ext: str) -> None:
    remove_logo()
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / f"logo.{ext}").write_bytes(content)


def remove_logo() -> None:
    for ext in ("png", "jpg", "webp"):
        (DATA_DIR / f"logo.{ext}").unlink(missing_ok=True)
