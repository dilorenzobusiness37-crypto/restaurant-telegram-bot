"""SQLite storage for table bookings, shared by the bot and the owner dashboard."""

import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "bookings.db"

# Bookings added by the owner from the dashboard (e.g. phone calls) have no Telegram user.
MANUAL_USER_ID = 0

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,  -- Telegram user id, or MANUAL_USER_ID
    username      TEXT,
    name          TEXT    NOT NULL,
    people        INTEGER NOT NULL,
    date          TEXT    NOT NULL,  -- ISO format YYYY-MM-DD
    time          TEXT    NOT NULL,  -- HH:MM
    notes         TEXT,
    status        TEXT    NOT NULL DEFAULT 'confirmed',  -- 'confirmed' | 'cancelled'
    created_at    TEXT    NOT NULL,
    source        TEXT    NOT NULL DEFAULT 'telegram',   -- 'telegram' | 'manual'
    phone         TEXT,
    cancelled_by  TEXT,              -- 'customer' | 'owner'
    cancelled_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_bookings_user ON bookings (user_id, date);
CREATE INDEX IF NOT EXISTS idx_bookings_date ON bookings (date, time);
"""

# Columns added after the first release: added to existing databases by init_db().
MIGRATIONS = {
    "source": "ALTER TABLE bookings ADD COLUMN source TEXT NOT NULL DEFAULT 'telegram'",
    "phone": "ALTER TABLE bookings ADD COLUMN phone TEXT",
    "cancelled_by": "ALTER TABLE bookings ADD COLUMN cancelled_by TEXT",
    "cancelled_at": "ALTER TABLE bookings ADD COLUMN cancelled_at TEXT",
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def init_db() -> None:
    """Create tables if they don't exist yet and upgrade older databases."""
    with closing(_connect()) as conn, conn:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(bookings)")}
        if existing:
            for column, statement in MIGRATIONS.items():
                if column not in existing:
                    conn.execute(statement)
        conn.executescript(SCHEMA)


def add_booking(
    user_id: int,
    username: str | None,
    name: str,
    people: int,
    booking_date: date,
    booking_time: str,
    notes: str | None,
    phone: str | None = None,
    source: str = "telegram",
) -> int:
    """Save a new booking and return its id."""
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            """INSERT INTO bookings
                   (user_id, username, name, people, date, time, notes, created_at, source, phone)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                username,
                name,
                people,
                booking_date.isoformat(),
                booking_time,
                notes,
                _now(),
                source,
                phone,
            ),
        )
        return cur.lastrowid


def get_upcoming_bookings(user_id: int, today: date) -> list[sqlite3.Row]:
    """Return the user's confirmed bookings from today onwards, soonest first."""
    with closing(_connect()) as conn:
        return conn.execute(
            """SELECT * FROM bookings
               WHERE user_id = ? AND status = 'confirmed' AND date >= ?
               ORDER BY date, time""",
            (user_id, today.isoformat()),
        ).fetchall()


def get_bookings_between(
    start: date, end: date, include_cancelled: bool = False
) -> list[sqlite3.Row]:
    """Return bookings from start to end (both included), by date and time."""
    status_filter = "" if include_cancelled else "AND status = 'confirmed'"
    with closing(_connect()) as conn:
        return conn.execute(
            f"""SELECT * FROM bookings
                WHERE date BETWEEN ? AND ? {status_filter}
                ORDER BY date, time, id""",
            (start.isoformat(), end.isoformat()),
        ).fetchall()


def get_booking(booking_id: int) -> sqlite3.Row | None:
    with closing(_connect()) as conn:
        return conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()


def cancel_booking(booking_id: int, user_id: int) -> bool:
    """Cancel a booking on behalf of the customer. Only the user who made it can cancel it.

    Returns True if a booking was actually cancelled.
    """
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            """UPDATE bookings SET status = 'cancelled', cancelled_by = 'customer', cancelled_at = ?
               WHERE id = ? AND user_id = ? AND status = 'confirmed'""",
            (_now(), booking_id, user_id),
        )
        return cur.rowcount > 0


def cancel_booking_by_owner(booking_id: int) -> bool:
    """Cancel any confirmed booking (owner dashboard). Returns True if it was cancelled."""
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            """UPDATE bookings SET status = 'cancelled', cancelled_by = 'owner', cancelled_at = ?
               WHERE id = ? AND status = 'confirmed'""",
            (_now(), booking_id),
        )
        return cur.rowcount > 0
