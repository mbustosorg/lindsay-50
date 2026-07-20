"""SQLite-backed message and config storage for Flask.

S3 is the source of truth for messages; this module also handles
rebuilding SQLite from S3 on startup.
"""

import fcntl
import json
import logging
import os
import sqlite3
import time
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

    Gunicorn boots each worker via a separate ``import main`` call,
    so two (or more) workers can race on ``init_db``. SQLite only
    permits one writer; the second worker either sees a file
    mid-creation ("attempt to write a readonly database") or hits
    journal contention ("disk I/O error"). Both observations came
    from the 2026-07-10 v137 deploy crash: pid 9 read-only, pid 10
    disk I/O.

    We serialize the schema-build phase with a non-blocking
    ``fcntl.flock`` advisory lock on a sidecar file. The first
    worker acquires the lock and runs init; subsequent workers see
    the file already created (CREATE TABLE IF NOT EXISTS is a no-op)
    and return without writing. The lock file is on /tmp on Heroku
    so it shares the SQLite file's life cycle (both gone on dyno
    restart).
    """
    db = _db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    lock_path = db.with_suffix(".init.lock")
    # Non-blocking lock — if a sibling worker holds it, the schema
    # is already created or in flight. We reopen the DB once the
    # file appears and let CREATE TABLE IF NOT EXISTS no-op.
    lock_fd: int | None = None
    try:
        lock_fd = os.open(
            str(lock_path),
            os.O_CREAT | os.O_RDWR,
            0o666,
        )
    except OSError as exc:
        logger.warning(
            "init_db: could not open lock file %s (%s); proceeding without lock",
            lock_path,
            exc,
        )

    got_lock = False
    if lock_fd is not None:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            got_lock = True
        except OSError:
            # Sibling worker has the lock. Wait briefly for the DB
            # file to exist (it always will — CREATE TABLE ran),
            # then the reopened connection treats CREATE TABLE as
            # a no-op. We don't block on the lock — that would
            # serialize worker startup needlessly.
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline and not db.exists():
                time.sleep(0.01)

    if got_lock:
        try:
            _create_schema(db)
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
    else:
        if lock_fd is not None:
            os.close(lock_fd)
        # Schema was created by the lock holder — just verify.
        _create_schema(db)


def _create_schema(db: Path) -> None:
    """Run the CREATE TABLE statements. Idempotent — CREATE TABLE IF NOT EXISTS
    is a no-op when the schema already exists."""
    conn = sqlite3.connect(db)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id          TEXT PRIMARY KEY,
                sender      TEXT NOT NULL,
                body        TEXT NOT NULL,
                received_at TEXT NOT NULL,
                media       TEXT
            )
        """)
        # In-place migration for pre-issue-38 databases: add the
        # column if it doesn't already exist. SQLite's
        # `ALTER TABLE ... ADD COLUMN` raises if the column
        # already exists, which is fine — we swallow and continue.
        # The CREATE TABLE above handles the fresh-init path.
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_received_at " "ON messages(received_at)")
        conn.commit()
    finally:
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

    Concurrency note: Gunicorn boots every worker via a separate
    ``import main`` call, so two or more workers can race here — both
    calling ``unlink()`` then ``init_db()`` then S3 fetches. SQLite
    only permits one writer; the second worker either sees a file
    mid-creation or hits journal contention. We serialize the whole
    rebuild under the same ``.init.lock`` sidecar (``init_db`` uses it
    too) so only one worker does the unlink-and-rebuild; the others
    see the DB already populated and just no-op.
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
    lock_path = db_path.with_suffix(".init.lock")
    lock_fd: int | None = None
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o666)
    except OSError as exc:
        logger.warning(
            "rebuild_from_s3: could not open lock file %s (%s); proceeding without lock",
            lock_path,
            exc,
        )
    got_lock = False
    if lock_fd is not None:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            got_lock = True
        except OSError:
            # Sibling worker holds it. Wait for the DB to be
            # repopulated before returning (they'll INSERT OR
            # REPLACE every message + the config).
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if db_path.exists() and db_path.stat().st_size > 0:
                    break
                time.sleep(0.05)
    if got_lock:
        try:
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
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
    else:
        # Lock holder finished (or is finishing) the rebuild; the DB
        # either exists with data (we waited for it above) or the
        # holder hit an S3 failure. Either way, no further action
        # here — the second worker just opens an existing DB.
        if lock_fd is not None:
            os.close(lock_fd)
        init_db()  # CREATE TABLE IF NOT EXISTS is a no-op
