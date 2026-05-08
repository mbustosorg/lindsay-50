"""SQLite storage layer for messages and config. Used by Flask."""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib.models import AllowedSender, Config, FilterRule, Message, RenderingConfig, SignConfig

DB_PATH = Path(__file__).parent.parent / "db.sqlite"


def init_db() -> None:
    """Create tables if they don't exist."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                sender TEXT NOT NULL,
                body TEXT NOT NULL,
                received_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                data TEXT NOT NULL
            )
            """
        )
        # Insert default config if missing
        cur = conn.execute("SELECT id FROM config WHERE id = 1")
        if cur.fetchone() is None:
            conn.execute(
                "INSERT INTO config (id, data) VALUES (1, ?)",
                (json.dumps(Config.default().to_dict()),),
            )


def put_message(sender: str, body: str) -> Message:
    """Store an inbound message and return the Message record."""
    msg = Message(
        id=str(uuid.uuid4()),
        sender=sender,
        body=body,
        received_at=datetime.now(timezone.utc),
    )
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO messages (id, sender, body, received_at) VALUES (?, ?, ?, ?)",
            (msg.id, msg.sender, msg.body, msg.received_at.isoformat()),
        )
    return msg


def get_all_messages() -> list[Message]:
    """Return all messages ordered by received_at ascending."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, sender, body, received_at FROM messages ORDER BY received_at ASC"
        ).fetchall()
    return [Message.from_row(tuple(r)) for r in rows]


def get_messages_since(since: datetime) -> list[Message]:
    """Return messages received after `since`, ordered by received_at ascending."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, sender, body, received_at FROM messages WHERE received_at > ? ORDER BY received_at ASC",
            (since.isoformat(),),
        ).fetchall()
    return [Message.from_row(tuple(r)) for r in rows]


def get_message(id: str) -> Message | None:
    """Return a single message by id, or None if not found."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, sender, body, received_at FROM messages WHERE id = ?", (id,)
        ).fetchone()
    if row is None:
        return None
    return Message.from_row(tuple(row))


def put_config(config: Config) -> None:
    """Persist the full config object to SQLite."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE config SET data = ? WHERE id = 1", (json.dumps(config.to_dict()),)
        )


def get_config() -> Config:
    """Load the config from SQLite, returning a default if none is stored."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT data FROM config WHERE id = 1").fetchone()
    if row is None:
        return Config.default()
    return Config.from_dict(json.loads(row[0]))
