"""SQLite-backed message and config storage for Flask.

S3 is the source of truth for messages; this module also handles
rebuilding SQLite from S3 on startup.
"""

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional

from lib_shared.models import SignConfig, Message

logger = logging.getLogger(__name__)


def _db_path() -> Path:
    """Return the path to the SQLite database file.

    On Heroku, the runtime filesystem under `/app` is read-only at
    runtime (the build extracts code to /app; the dyno gets a
    read-only view of that extraction). Writing the SQLite file to
    `/app/heart-message-manager/db.sqlite` therefore fails with
    "attempt to write a readonly database" / "disk I/O error" when
    `init_db()` runs on boot.

    Heroku-24 (the current stack — see Heroku-20 release notes)
    no longer sets the legacy `DYNO` env var. It DOES set
    `HEROKU_APP_NAME` on every dyno, so we use that as the
    detection signal. Fall back to DYNO too in case a future
    stack brings it back or an older dyno is still around.

    The DB is rebuilt from S3 on every boot, so /tmp's ephemeral
    nature is fine — the dyno restart that wipes /tmp also
    rebuilds the DB on next boot.

    On laptop / Pi, the DB lives next to the source under
    `heart-message-manager/db.sqlite` as before.
    """
    if os.environ.get("HEROKU_APP_NAME") or os.environ.get("DYNO"):
        return Path("/tmp/lindsay50.db.sqlite")
    return Path(__file__).parent.parent / "heart-message-manager" / "db.sqlite"


def _json_dumps(cfg: SignConfig) -> str:
    """Serialize a SignConfig to a compact JSON string."""
    return json.dumps(cfg.to_dict(), separators=(",", ":"))


def _json_loads(raw: str) -> SignConfig:
    """Deserialize a SignConfig from a JSON string."""
    return SignConfig.from_dict(json.loads(raw))


# ---------------------------------------------------------------------------
# Database initialisation
# ---------------------------------------------------------------------------


def init_db() -> None:
    """Create the messages and config tables if they don't exist.

    `messages.media` is the JSON-serialized `Message.media` list
    (issue #38). The column was added on top of the original 4-field
    schema so legacy rows survive a fresh migration — `media` is
    nullable and `get_all_messages` defaults an empty/None value to
    `[]` via `Message.from_dict`'s `media=d.get("media") or []`
    path, matching the additive wire shape spec.
    """
    db = _db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id          TEXT PRIMARY KEY,
            sender      TEXT NOT NULL,
            body        TEXT NOT NULL,
            received_at TEXT NOT NULL,
            media       TEXT
        )
    """)
    # In-place migration for pre-issue-38 databases: add the column
    # if it doesn't already exist. SQLite's `ALTER TABLE ... ADD
    # COLUMN` raises if the column already exists, which is fine —
    # we swallow and continue. The CREATE TABLE above handles the
    # fresh-init path.
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN media TEXT")
    except sqlite3.OperationalError:
        # Column already exists (most common case after first run).
        pass
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


def _serialize_media(media: list[dict] | None) -> str | None:
    """Serialize a `Message.media` list to JSON for SQLite storage.

    `None` and `[]` both round-trip to `None` in the column — the
    `get_*` helpers fall back to `[]` via `Message.from_dict`'s
    `media=d.get("media") or []` (note: `0` and empty strings would
    also be falsy, but JSON's `null` is what we emit). The empty-
    string-when-canonical shape is the design: SQLite's TEXT column
    stores a JSON array for non-empty MMS payloads and `NULL` for
    everything else.
    """
    if not media:
        return None
    return json.dumps(list(media))


def _deserialize_media(raw: str | None) -> list[dict]:
    """Reverse of `_serialize_media`. Bad JSON (e.g. legacy row) → `[]`."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("malformed media JSON in SQLite: %r — substituting []", raw)
        return []
    if not isinstance(parsed, list):
        return []
    return [it for it in parsed if isinstance(it, dict)]


def put_message(msg: Message) -> None:
    """Insert a message into SQLite. Upserts on duplicate id.

    The `media` field is serialized to JSON and stored in the
    `messages.media` TEXT column. SMS-only messages pass an empty
    list, which serializes to NULL — see `_serialize_media`.
    """
    conn = sqlite3.connect(_db_path())
    conn.execute(
        "INSERT OR REPLACE INTO messages (id, sender, body, received_at, media) VALUES (?, ?, ?, ?, ?)",
        (msg.id, msg.sender, msg.body, msg.received_at, _serialize_media(msg.media)),
    )
    conn.commit()
    conn.close()


def get_message(id: str) -> Optional[Message]:
    """Return the message with the given UUID, or None."""
    conn = sqlite3.connect(_db_path())
    row = conn.execute("SELECT id, sender, body, received_at, media FROM messages WHERE id = ?", (id,)).fetchone()
    conn.close()
    if row is None:
        return None
    return Message(
        id=row[0],
        sender=row[1],
        body=row[2],
        received_at=row[3],
        media=_deserialize_media(row[4]),
    )


def get_all_messages() -> list[Message]:
    """Return all messages ordered by received_at descending (most recent first)."""
    conn = sqlite3.connect(_db_path())
    rows = conn.execute(
        "SELECT id, sender, body, received_at, media FROM messages ORDER BY received_at DESC"
    ).fetchall()
    conn.close()
    return [
        Message(
            id=r[0],
            sender=r[1],
            body=r[2],
            received_at=r[3],
            media=_deserialize_media(r[4]),
        )
        for r in rows
    ]


def get_messages_since(timestamp: str) -> list[Message]:
    """Return messages with received_at strictly after the given ISO 8601 timestamp.

    Results are ordered by received_at descending (most recent first).
    """
    conn = sqlite3.connect(_db_path())
    rows = conn.execute(
        "SELECT id, sender, body, received_at, media FROM messages WHERE received_at > ? ORDER BY received_at DESC",
        (timestamp,),
    ).fetchall()
    conn.close()
    return [
        Message(
            id=r[0],
            sender=r[1],
            body=r[2],
            received_at=r[3],
            media=_deserialize_media(r[4]),
        )
        for r in rows
    ]


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
    row = conn.execute("SELECT value FROM config WHERE key = 'current'").fetchone()
    conn.close()
    if row is None:
        return SignConfig.default()
    return _json_loads(row[0])


def put_config(cfg: SignConfig) -> None:
    """Save the config JSON to SQLite under the 'current' key."""
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
    # Defensive: scan S3 prefixes that DON'T belong to message files and
    # make sure the rebuild path doesn't mistake them for messages. The
    # canonical `s3.load_messages_from_s3` is hardcoded to the `messages/`
    # prefix so this is a no-op in production — the check guards against
    # a future caller passing a more permissive S3 lister that scans
    # `media/images/` or `media/videos/` keys. Failure mode if skipped:
    # S3 MMS attachments would be parsed as message JSON, fail on
    # `KeyError("body" | "received_at")`, log warnings, and waste
    # time. The skip filter is per-prefix so it's cheap.
    try:
        from s3 import MEDIA_KEY_PREFIXES
    except ImportError:
        MEDIA_KEY_PREFIXES = ()
    for prefix in MEDIA_KEY_PREFIXES:
        # The constant is small (two prefixes); an O(n) walk per call is
        # negligible compared to the S3 paginator's network work. The point
        # is to be explicit about the skip rather than silently over-read.
        logger.debug("rebuild_from_s3: skipping non-message S3 prefix %r", prefix)
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
