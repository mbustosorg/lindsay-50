"""SQLite-backed message and config storage for Flask.

S3 is the source of truth for messages; this module also handles
rebuilding SQLite from S3 on startup.
"""

import json
import sqlite3
from pathlib import Path
from typing import Optional

from lib_shared.models import SignConfig, Message


def _db_path() -> Path:
    return Path(__file__).parent.parent / "heart-message-manager" / "db.sqlite"


def _json_dumps(cfg: SignConfig) -> str:
    return json.dumps(cfg.to_dict(), separators=(",", ":"))


def _json_loads(raw: str) -> SignConfig:
    return SignConfig.from_dict(json.loads(raw))


# ---------------------------------------------------------------------------
# Database initialisation
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create the messages and config tables if they don't exist."""
    db = _db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id          TEXT PRIMARY KEY,
            sender      TEXT NOT NULL,
            body        TEXT NOT NULL,
            received_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    # Index for time-ordered retrieval
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_received_at ON messages(received_at)")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Message operations
# ---------------------------------------------------------------------------

def put_message(msg: Message) -> None:
    """Insert a message into SQLite. Upserts on duplicate id."""
    conn = sqlite3.connect(_db_path())
    conn.execute(
        "INSERT OR REPLACE INTO messages (id, sender, body, received_at) VALUES (?, ?, ?, ?)",
        (msg.id, msg.sender, msg.body, msg.received_at),
    )
    conn.commit()
    conn.close()


def get_message(id: str) -> Optional[Message]:
    """Return the message with the given UUID, or None."""
    conn = sqlite3.connect(_db_path())
    row = conn.execute(
        "SELECT id, sender, body, received_at FROM messages WHERE id = ?", (id,)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return Message(id=row[0], sender=row[1], body=row[2], received_at=row[3])


def get_all_messages() -> list[Message]:
    """Return all messages ordered by received_at descending (most recent first)."""
    conn = sqlite3.connect(_db_path())
    rows = conn.execute(
        "SELECT id, sender, body, received_at FROM messages ORDER BY received_at DESC"
    ).fetchall()
    conn.close()
    return [Message(id=r[0], sender=r[1], body=r[2], received_at=r[3]) for r in rows]


def get_messages_since(timestamp: str) -> list[Message]:
    """Return messages with received_at strictly after the given ISO 8601 timestamp.

    Results are ordered by received_at descending (most recent first).
    """
    conn = sqlite3.connect(_db_path())
    rows = conn.execute(
        "SELECT id, sender, body, received_at FROM messages WHERE received_at > ? ORDER BY received_at DESC",
        (timestamp,),
    ).fetchall()
    conn.close()
    return [Message(id=r[0], sender=r[1], body=r[2], received_at=r[3]) for r in rows]


def message_count() -> int:
    """Return total number of messages stored."""
    conn = sqlite3.connect(_db_path())
    row = conn.execute("SELECT COUNT(*) FROM messages").fetchone()
    conn.close()
    return row[0] if row else 0


# ---------------------------------------------------------------------------
# Config operations
# ---------------------------------------------------------------------------

def get_config() -> SignConfig:
    """Return the current config, or a default config if none is stored."""
    conn = sqlite3.connect(_db_path())
    row = conn.execute(
        "SELECT value FROM config WHERE key = 'current'"
    ).fetchone()
    conn.close()
    if row is None:
        return SignConfig.default()
    return _json_loads(row[0])


def put_config(cfg: SignConfig) -> None:
    """Save the config JSON to SQLite under the 'current' key.

    tz_offset_mins is recomputed from the current timezone so it is always
    up to date when serialized.
    """
    from lib.time import tz_offset_mins
    cfg.tz_offset_mins = tz_offset_mins(cfg.timezone)
    conn = sqlite3.connect(_db_path())
    conn.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES ('current', ?)",
        (_json_dumps(cfg),),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# S3 rebuild helpers (called on startup)
# ---------------------------------------------------------------------------

import logging

logger = logging.getLogger(__name__)


def rebuild_from_s3(s3_load_messages, s3_load_config) -> None:
    """Wipe SQLite, then reload both config and messages from S3.

    Each loader runs in its own try/except so partial failures log but
    don't abort the other restore.

    Args:
        s3_load_messages: A callable that returns an iterator of Message objects,
                          e.g. ``s3.load_messages_from_s3()``.
        s3_load_config:  A callable that returns a config dict or None, e.g.
                         ``s3.load_latest_config``.
    """
    db_path = _db_path()
    try:
        db_path.unlink()
    except OSError:
        pass
    init_db()

    try:
        for msg in s3_load_messages():
            put_message(msg)
        logger.info("Rebuilt messages from S3")
    except Exception as e:
        logger.warning("Could not rebuild messages from S3: %s", e)

    try:
        cfg_data = s3_load_config()
        if cfg_data:
            put_config(SignConfig.from_dict(cfg_data))
            logger.info("Loaded config from S3 snapshot")
    except Exception as e:
        logger.warning("Could not rebuild config from S3: %s", e)
