"""
Local notes storage. Nothing in this file ever talks to Odoo -- it's a plain
SQLite database owned entirely by this app.
"""

import sqlite3
from datetime import datetime, timezone

DB_PATH = "warehouse_notes.db"

# The warehouse's own stages, tracked entirely in this local database.
# Edit this list to match your actual floor process -- order matters, it
# drives the progress display on the dashboard.
STATUS_OPTIONS = [
    "pending",
    "picking",
    "packed",
    "staged",
    "loaded",
    "out_for_delivery",
    "delivered",
]


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS delivery_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            picking_id INTEGER NOT NULL,
            picking_name TEXT NOT NULL,
            note_text TEXT NOT NULL,
            author TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notes_picking_id ON delivery_notes(picking_id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS local_status (
            picking_id INTEGER PRIMARY KEY,
            picking_name TEXT NOT NULL,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def add_note(picking_id: int, picking_name: str, note_text: str, author: str = ""):
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO delivery_notes (picking_id, picking_name, note_text, author, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (picking_id, picking_name, note_text, author, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def get_notes_for_picking(picking_id: int):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM delivery_notes WHERE picking_id = ? ORDER BY created_at DESC",
        (picking_id,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_note_counts():
    """Returns {picking_id: count} for all pickings that have at least one note."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT picking_id, COUNT(*) as cnt FROM delivery_notes GROUP BY picking_id"
    ).fetchall()
    conn.close()
    return {row["picking_id"]: row["cnt"] for row in rows}


def set_status(picking_id: int, picking_name: str, status: str):
    if status not in STATUS_OPTIONS:
        raise ValueError(f"'{status}' is not a recognized status: {STATUS_OPTIONS}")
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO local_status (picking_id, picking_name, status, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(picking_id) DO UPDATE SET
            status = excluded.status,
            updated_at = excluded.updated_at
        """,
        (picking_id, picking_name, status, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def get_all_statuses():
    """Returns {picking_id: {"status": ..., "updated_at": ...}} for every order that has one set."""
    conn = get_connection()
    rows = conn.execute("SELECT * FROM local_status").fetchall()
    conn.close()
    return {row["picking_id"]: dict(row) for row in rows}
