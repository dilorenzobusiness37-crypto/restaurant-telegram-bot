"""SQLite storage for table bookings."""

import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "bookings.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    username    TEXT,
    name        TEXT    NOT NULL,
    people      INTEGER NOT NULL,
    date        TEXT    NOT NULL,  -- ISO format YYYY-MM-DD
    time        TEXT    NOT NULL,  -- HH:MM
    notes       TEXT,
    status      TEXT    NOT NULL DEFAULT 'confirmed',  -- 'confirmed' | 'cancelled'
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bookings_user ON bookings (user_id, date);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables if they don't exist yet."""
    with closing(_connect()) as conn, conn:
        conn.executescript(SCHEMA)


def add_booking(
    user_id: int,
    username: str | None,
    name: str,
    people: int,
    booking_date: date,
    booking_time: str,
    notes: str | None,
) -> int:
    """Save a new booking and return its id."""
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            """INSERT INTO bookings (user_id, username, name, people, date, time, notes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                username,
                name,
                people,
                booking_date.isoformat(),
                booking_time,
                notes,
                datetime.now().isoformat(timespec="seconds"),
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


def get_booking(booking_id: int) -> sqlite3.Row | None:
    with closing(_connect()) as conn:
        return conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()


def cancel_booking(booking_id: int, user_id: int) -> bool:
    """Cancel a booking. Only the user who made it can cancel it.

    Returns True if a booking was actually cancelled.
    """
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            """UPDATE bookings SET status = 'cancelled'
               WHERE id = ? AND user_id = ? AND status = 'confirmed'""",
            (booking_id, user_id),
        )
        return cur.rowcount > 0
