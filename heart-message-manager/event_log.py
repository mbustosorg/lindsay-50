"""Browser-side event log for the dashboard's in-memory selector (issue #48).

Mirrors the Pi-side `heart-matrix-controller/event_log.py` contract
(`append(event)` / `query(...)` / `last_for(message_id, event_type)` /
`reload()` / `__len__` / `max_entries`) but backs the log with an
in-memory `collections.deque(maxlen=N)`. The dashboard owns one
`EventLog` instance per generation; `Stop` and `Start` each discard the
prior queue and construct a fresh one — the in-memory log does NOT
persist across generations, refreshes, or browser restarts.

Schema contract (locked — same as the Pi's JSONL `EventLog`):

    {
        "event_type":   "text_display" | ...,
        "message_id":   "<uuid>",
        "timestamp":    1752080123.45,
        "received_at":  1752000000.0
    }

No mutable fields (no `favorite`). Forward-compatible with a future
MQTT publication of the Pi's events to the browser.

Why a deque-backed log instead of IndexedDB (per the issue #48 spec):

  - The dashboard simulator is meant to be a fresh-start, stop/reset
    artifact. Persisting the event log across page reloads would
    carry selection history into a generation that was supposed to be
    brand new — exactly the kind of "did it actually reset?" bug
    that's hard to track down.
  - IndexedDB's async API also fights the synchronous `append` /
    `query` surface the `MessageSelector` already calls. A `deque`
    makes every operation O(1) on append and O(n) bounded on query,
    with no IDB round-trip and no callback hop.
  - The `EventLog` cap (default 100) is the FIFO ring — `deque(maxlen=N)`
    drops the oldest entry at the cap with no extra bookkeeping.

This module deliberately does NOT subclass the Pi-side `EventLog`
directly (different file paths, different import surfaces). It
implements the same contract — `MessageSelector` consumes either
backend through the same surface, so the rest of the runtime doesn't
need to know.
"""

from __future__ import annotations

import logging
import threading
from collections import deque

log = logging.getLogger("heart")

# Required keys per event. Kept in lockstep with the Pi-side schema in
# `heart-matrix-controller/event_log.py` so the same selector class
# works against both backends.
_REQUIRED_KEYS = ("event_type", "message_id", "timestamp", "received_at")

# Default cap on the deque. Matches the Pi-side default and the
# previous IndexedDB-backed default. Override per-instance via the
# `max_entries` constructor kwarg.
DEFAULT_MAX_ENTRIES = 100


class EventLog:
    """Append-only in-memory event log backed by `collections.deque(maxlen=N)`.

    Implements the same surface the Pi's JSONL `EventLog` exposes so the
    `MessageSelector` class can consume either backend without change.
    The queue is bounded (default 100 entries) and discards the oldest
    entry at the cap — FIFO eviction.

    Thread-safety: a reentrant lock guards mutations and reads. PyScript
    runs on the browser's event loop, but the in-memory dashboard
    controller may still see Python-side calls from JS-driven
    coroutines that race against the dispatch thread on heavy bursts.

    Args:
        max_entries: Bounded-ring cap. Older entries are dropped when
            the cap is reached. Must be a positive integer. Default
            100 — matches the Pi-side default.
    """

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        """Construct an empty deque-backed event log."""
        if max_entries < 1:
            raise ValueError(f"max_entries must be a positive integer, got {max_entries}")
        self._max_entries = int(max_entries)
        self._events: deque = deque(maxlen=self._max_entries)
        self._lock = threading.RLock()

    # --- Public API (matches heart-matrix-controller/event_log.py) ---

    def append(self, event: dict) -> None:
        """Append one event to the log.

        Silently drops events missing the required keys (so a corrupt
        upstream cannot poison the queue). The deque's `maxlen`
        evicts the oldest entry at the cap.
        """
        cleaned = self._clean_event(event)
        if cleaned is None:
            log.warning("EventLog.append rejected event: %r", event)
            return
        with self._lock:
            self._events.append(cleaned)

    def query(
        self,
        event_type: str | None = None,
        message_id: str | None = None,
        since: float | None = None,
    ) -> list[dict]:
        """Return events matching the filters, in append order.

        Reads from the in-memory queue (no I/O). The deque is bounded
        to `max_entries` entries, so the result list is always small.

        Returns:
            List of event dicts in append order. Empty list when no
            events match.
        """
        with self._lock:
            snapshot = list(self._events)
        out: list[dict] = []
        for event in snapshot:
            if event_type is not None and event.get("event_type") != event_type:
                continue
            if message_id is not None and event.get("message_id") != message_id:
                continue
            if since is not None and float(event.get("timestamp", 0.0)) < since:
                continue
            out.append(event)
        return out

    def last_for(self, message_id: str, event_type: str):
        """Return the most recent event for `(message_id, event_type)` or None.

        Convenience wrapper around `query()` for the common
        "when was this message last shown as text_display?" lookup.
        Mirrors the Pi-side `EventLog.last_for` signature.
        """
        latest = None
        for event in self.query(event_type=event_type, message_id=message_id):
            latest = event
        return latest

    def reload(self) -> None:
        """No-op for the in-memory backend.

        The deque holds the canonical state. Retained for API
        compatibility with the Pi-side `EventLog.reload` so callers
        can swap backends without branching.
        """
        return None

    def clear(self) -> None:
        """Drop every event from the queue.

        Called by the dashboard controller during Stop teardown so
        the next generation's log starts empty without keeping a
        reference to the prior generation's instance. The deque
        itself is replaced (not mutated in place) so any external
        reference that outlives the generation sees an empty queue.
        """
        with self._lock:
            self._events = deque(maxlen=self._max_entries)

    def __len__(self) -> int:
        """Number of events currently in the queue."""
        with self._lock:
            return len(self._events)

    @property
    def max_entries(self) -> int:
        """The bounded-ring cap."""
        return self._max_entries

    # --- Internal helpers ---

    @staticmethod
    def _clean_event(event) -> dict | None:
        """Return a new dict with exactly the four required keys, or None.

        Silently drops any extra fields (including `favorite`) so a caller
        that accidentally passes a richer dict cannot pollute the schema.
        Returns None when any required key is absent or when the input is
        not a dict-like object.
        """
        if event is None or not isinstance(event, dict):
            return None
        try:
            for key in _REQUIRED_KEYS:
                if key not in event:
                    return None
            return {
                "event_type": str(event["event_type"]),
                "message_id": str(event["message_id"]),
                "timestamp": float(event["timestamp"]),
                "received_at": float(event["received_at"]),
            }
        except Exception:
            return None
