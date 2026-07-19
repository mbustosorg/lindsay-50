"""Append-only JSONL event log for the Pi's display-recency tracker (issue #26).

The Pi writes one event per message advance to ``data/events.jsonl`` (default
path, configurable via ``EVENT_LOG_PATH`` in ``settings.toml``). The selector
reads the log to compute ``display_recency`` for each candidate message. Each
event carries ONLY immutable facts about the render itself:

    {
        "event_type":   "text_display" | "image_display" | "video_display" | ...,
        "message_id":   "<uuid>",
        "timestamp":    1752080123.45,   # epoch seconds, render time
        "received_at":  1752000000.0     # epoch seconds, denormalized from message
    }

``favorite`` is intentionally NOT in the schema — favorite is a mutable
current-state property of the message (it can change between events), so it is
read from the message record at pick time, not captured in the historical log.

The log is a bounded ring of the most recent N entries (default 100). When the
log is at capacity, appending a new event drops the oldest entry and rewrites
the on-disk file with the N most recent entries — no archive, no compression.
This bounds disk usage at N × ~80 bytes ≈ 8 KB.

The in-memory cache (``self._events``) is loaded at boot and refreshed on every
append (write-through). The selector reads from the cache, not the file
directly, so pick latency is O(matching-events-for-this-pattern-and-id),
typically O(1) per candidate.

Corrupt-line tolerance: any line that fails JSON parsing is skipped and a
warning is logged. One bad line MUST NOT lose other events.

The file is rewritten atomically on every eviction (write to a temp file,
``os.replace``) so a crash mid-rewrite cannot leave a half-empty log.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from typing import Iterator, Optional

log = logging.getLogger("heart")

# Required keys for an event. Mutable current-state fields (favorite, etc.)
# are deliberately NOT here — see module docstring.
_REQUIRED_KEYS = frozenset({"event_type", "message_id", "timestamp", "received_at"})


class EventLog:
    """Append-only JSONL event log backed by a single file on the Pi's disk.

    The log is bounded to the most recent ``max_entries`` lines (default 100,
    configurable via ``EVENT_LOG_MAX_ENTRIES`` in ``settings.toml``). When the
    log is at capacity, the oldest entry is dropped on append and the on-disk
    file is rewritten with the N most recent entries (in append order).

    The in-memory cache is loaded once at construction time and refreshed on
    every ``append()``. Reads (``query()``) operate on the cache — the file is
    only re-read if the caller explicitly calls ``reload()``.

    Corrupt lines are skipped at load time (with a warning) and at append time
    (the new event is appended verbatim, not parsed). One bad line cannot lose
    other events.

    Thread-safety: ``append()`` and ``query()`` are guarded by a reentrant
    lock so a concurrent append + read sees a consistent snapshot. The lock is
    reentrant so callers that already hold it (e.g. tests) can call into
    multiple methods without deadlocking.
    """

    def __init__(
        self,
        path: str = "data/events.jsonl",
        max_entries: int = 100,
    ) -> None:
        """Initialize the event log at ``path`` with a bounded ring of size N.

        Args:
            path: Filesystem path to the JSONL log file. Created if missing.
                Parent directories are created lazily on the first write.
            max_entries: Maximum number of entries to retain. When the log
                reaches this size, the oldest entry is dropped on append.
                Must be a positive integer.

        Raises:
            ValueError: if ``max_entries < 1``.
        """
        if max_entries < 1:
            raise ValueError(f"max_entries must be a positive integer, got {max_entries}")
        self._path = path
        self._max_entries = int(max_entries)
        self._events: list[dict] = []
        self._lock = threading.RLock()
        self._load()

    # --- Public API ---

    def append(self, event: dict) -> None:
        """Append one event to the log.

        The event MUST be a dict containing exactly the four required keys
        (``event_type``, ``message_id``, ``timestamp``, ``received_at``) — no other
        fields are stored on disk or in memory. Any extra fields are silently
        dropped so a caller that accidentally passes ``favorite`` cannot
        pollute the schema.

        If the log is at capacity, the oldest entry is dropped and the
        on-disk file is rewritten with the N most recent entries.

        Args:
            event: A dict with the four required keys (see module docstring).
                ``timestamp`` and ``received_at`` are floats (epoch seconds).
                ``message_id`` is a string. ``event_type`` is a string
                discriminator (e.g. ``"text_display"``).
        """
        cleaned = _clean_event(event)
        if cleaned is None:
            log.warning("EventLog.append rejected event missing required keys: %r", event)
            return
        with self._lock:
            self._events.append(cleaned)
            if len(self._events) > self._max_entries:
                self._events = self._events[-self._max_entries :]
            self._rewrite_locked()

    def query(
        self,
        event_type: Optional[str] = None,
        message_id: Optional[str] = None,
        since: Optional[float] = None,
    ) -> Iterator[dict]:
        """Yield events matching the filters, in append order.

        All filters are AND'd. None / unset means "don't filter on this
        dimension". Events are returned as live references to the in-memory
        cache — callers should NOT mutate them. The cache is append-only and
        bounded, so a reference held by the caller is safe for the lifetime
        of the EventLog.

        Args:
            event_type: If set, only events whose ``event_type`` matches.
            message_id: If set, only events whose ``message_id`` matches.
            since: If set, only events whose ``timestamp >= since``.
                Epoch seconds.

        Yields:
            Event dicts in append order (oldest first).
        """
        with self._lock:
            snapshot = list(self._events)
        for event in snapshot:
            if event_type is not None and event.get("event_type") != event_type:
                continue
            if message_id is not None and event.get("message_id") != message_id:
                continue
            if since is not None and float(event.get("timestamp", 0.0)) < since:
                continue
            yield event

    def last_for(
        self,
        message_id: str,
        event_type: str,
    ) -> Optional[dict]:
        """Return the most recent event for ``(message_id, event_type)`` or None.

        Convenience wrapper around ``query()`` for the common "when was this
        message last shown as a text_display?" lookup. O(n) over the cache
        but the cache is bounded at N entries (default 100) and the lookup
        is only called during a pick, not on a timer.

        Args:
            message_id: The message id to look up.
            event_type: The event_type discriminator (e.g. ``"text_display"``).

        Returns:
            The most recent matching event dict, or None if no event matches.
        """
        latest: Optional[dict] = None
        for event in self.query(event_type=event_type, message_id=message_id):
            latest = event
        return latest

    def reload(self) -> None:
        """Re-read the log file from disk into the in-memory cache.

        Useful after an external process rewrote the file (e.g. a manual edit
        during debugging). The on-disk file is the source of truth; the
        in-memory cache is just an optimization for reads.
        """
        with self._lock:
            self._load_locked()

    def __len__(self) -> int:
        """Number of events currently in the in-memory cache."""
        with self._lock:
            return len(self._events)

    @property
    def path(self) -> str:
        """The on-disk file path this log reads/writes."""
        return self._path

    @property
    def max_entries(self) -> int:
        """The bounded-ring cap."""
        return self._max_entries

    # --- Internals ---

    def _load(self) -> None:
        """Load the log file into the in-memory cache (initial)."""
        with self._lock:
            self._load_locked()

    def _load_locked(self) -> None:
        """Load the log file into the in-memory cache. Caller holds the lock.

        Skips corrupt lines (with a warning) and applies the bounded-ring
        cap by retaining the most recent ``_max_entries`` lines. The cap is
        applied here so a file written by an older build with a larger cap
        is correctly trimmed on the next read.
        """
        self._events = []
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError as e:
            log.warning("EventLog load failed: %s", e)
            return
        parsed: list[dict] = []
        for lineno, raw in enumerate(lines, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError as e:
                log.warning("EventLog skipping corrupt line %d in %s: %s", lineno, self._path, e)
                continue
            cleaned = _clean_event(event)
            if cleaned is None:
                log.warning(
                    "EventLog skipping line %d in %s: missing required keys %r",
                    lineno,
                    self._path,
                    sorted(_REQUIRED_KEYS),
                )
                continue
            parsed.append(cleaned)
        # Apply the cap: keep the most recent N entries (in append order).
        if len(parsed) > self._max_entries:
            parsed = parsed[-self._max_entries :]
        self._events = parsed

    def _rewrite_locked(self) -> None:
        """Rewrite the on-disk file with the current in-memory cache. Caller holds the lock.

        Atomic via temp-file + ``os.replace``: a crash mid-rewrite cannot
        leave a half-empty file. The parent directory is created lazily so
        the log can be initialized before the deploy creates ``data/``.
        """
        parent = os.path.dirname(self._path)
        if parent and not os.path.isdir(parent):
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as e:
                log.warning("EventLog mkdir failed for %s: %s", parent, e)
                return
        try:
            # ``delete=False`` + explicit unlink: ``os.replace`` overwrites
            # the destination atomically, but Windows refuses to replace an
            # open file. ``NamedTemporaryFile`` would also unlink on close
            # which is exactly what we want after the replace succeeds.
            fd, tmp_path = tempfile.mkstemp(prefix=".events.", suffix=".jsonl.tmp", dir=parent or None)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    for event in self._events:
                        f.write(json.dumps(event, separators=(",", ":")))
                        f.write("\n")
                os.replace(tmp_path, self._path)
            except Exception:
                # Best-effort cleanup of the temp file on any failure
                # before the replace.
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as e:
            log.warning("EventLog rewrite failed for %s: %s", self._path, e)


def _clean_event(event: object) -> Optional[dict]:
    """Return a new dict with exactly the four required keys, or None if missing.

    Silently drops any extra fields — including ``favorite`` and any other
    mutable current-state field — so a caller that accidentally passes a
    richer dict cannot pollute the schema. Returns None when any required
    key is absent or when the input is not a dict.
    """
    if not isinstance(event, dict):
        return None
    missing = [k for k in _REQUIRED_KEYS if k not in event]
    if missing:
        return None
    return {
        "event_type": str(event["event_type"]),
        "message_id": str(event["message_id"]),
        "timestamp": float(event["timestamp"]),
        "received_at": float(event["received_at"]),
    }
