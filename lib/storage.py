import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from .models import Message, Config

DB_PATH = Path(__file__).parent.parent / "heart-sms-receiver" / "db.sqlite"


def _get_db_path() -> Path:
    return DB_PATH


@contextmanager
def _get_conn():
    conn = sqlite3.connect(_get_db_path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Initialize the database schema."""
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id          TEXT PRIMARY KEY,
                sender      TEXT NOT NULL,
                body        TEXT NOT NULL,
                received_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS config (
                key    TEXT PRIMARY KEY,
                value  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_received_at ON messages(received_at);
        """)


def put_message(message: Message) -> None:
    """Store a message to the database."""
    with _get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO messages (id, sender, body, received_at) VALUES (?, ?, ?, ?)",
            (message.id, message.sender, message.body, message.received_at),
        )


def get_messages_since(since: Optional[str] = None) -> list[Message]:
    """Get all messages with received_at strictly after the given timestamp."""
    with _get_conn() as conn:
        if since is None:
            rows = conn.execute(
                "SELECT id, sender, body, received_at FROM messages ORDER BY received_at ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, sender, body, received_at FROM messages WHERE received_at > ? ORDER BY received_at ASC",
                (since,),
            ).fetchall()
        return [Message(id=r["id"], sender=r["sender"], body=r["body"], received_at=r["received_at"]) for r in rows]


def get_all_messages() -> list[Message]:
    """Get all messages, ordered by received_at ascending."""
    return get_messages_since(None)


def get_message(id: str) -> Optional[Message]:
    """Get a single message by ID."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id, sender, body, received_at FROM messages WHERE id = ?",
            (id,),
        ).fetchone()
        if row is None:
            return None
        return Message(id=row["id"], sender=row["sender"], body=row["body"], received_at=row["received_at"])


def put_config(config: Config) -> None:
    """Store config as JSON with key='current'."""
    with _get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            ("current", json.dumps(config.to_dict())),
        )


def get_config() -> Config:
    """Get the current config, returning default if none exists."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM config WHERE key = 'current'"
        ).fetchone()
        if row is None:
            return Config.default()
        return Config.from_dict(json.loads(row["value"]))
