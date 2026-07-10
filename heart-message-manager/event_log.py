"""Browser-side event log (PyScript wrapper) — issue #26.

Mirrors the Pi-side `heart-matrix-controller/event_log.py` schema but
backs the in-memory cache with IndexedDB via Pyodide's `js.indexedDB`
proxy. Used by the browser preview's `MessageSelector` instance so the
preview is self-consistent (the same selector class produces the same
pick for the same `(messages, now, event_log)` triple, regardless of
which runtime is asking). The preview does NOT replicate the Pi's log
— each browser has its own IndexedDB. Per the spec's "preview is
illustrative" contract.

This module is importable only inside a PyScript runtime (it depends
on `js.indexedDB`). The host CPython test suite mocks the `js` module
to exercise the wrapper without a real browser. The selector lives in
`lib_shared/selector.py` and runs in both runtimes unchanged — only
the event-log source differs (IndexedDB vs Pi-local JSONL).

Schema contract (locked — see `heart-matrix-controller/event_log.py`
module docstring for the full rationale):

    {
        "event_type":   "text_display" | ...,
        "message_id":   "<uuid>",
        "timestamp":    1752080123.45,
        "received_at":  1752000000.0
    }

No mutable fields (no `favorite`). The schema is forward-compatible
with a future MQTT publication of the Pi's events to the browser.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger("heart")

_DB_NAME = "lindsay50"
_DB_VERSION = 1
_STORE_NAME = "events"
# Required keys per event. Kept in lockstep with the Pi-side schema in
# `heart-matrix-controller/event_log.py` so the same selector class
# works against both backends.
_REQUIRED_KEYS = ("event_type", "message_id", "timestamp", "received_at")


def _hasattr_safe(obj, name):
    """hasattr that's safe against Pyodide JsProxy attribute access errors."""
    try:
        return hasattr(obj, name)
    except Exception:
        return False


def _to_py_safe(obj):
    """Call `obj.to_py()` if it exists; otherwise return obj unchanged."""
    if _hasattr_safe(obj, "to_py"):
        try:
            return obj.to_py()
        except Exception:
            return obj
    return obj


def _to_js_dict(d: dict):
    """Convert a Python dict to a JS-friendly object via Pyodide's to_js.

    Falls back to the bare Python dict when Pyodide's to_js is unavailable
    (e.g. in the host CPython test suite) — IDB calls simply skip in
    that mode, the in-memory cache still works.
    """
    try:
        from pyodide.ffi import to_js  # type: ignore[import-not-found]
        from js import Object as _js_object  # type: ignore[import-not-found]

        return to_js(d, dict_converter=_js_object.fromEntries)
    except Exception:
        return d


def _clean_event(event):
    """Return a new dict with exactly the four required keys, or None.

    Silently drops any extra fields (including `favorite`) so a caller
    that accidentally passes a richer dict cannot pollute the schema.
    Returns None when any required key is absent or when the input is
    not a dict-like object (IDB rows may come back as JS objects).
    """
    if event is None:
        return None
    try:
        # IDB rows arrive as JS objects; coerce to a Python dict via
        # `to_py()` when available. Pyodide's JsProxy supports both
        # attribute-style (`event.event_type`) and key-style
        # (`event["event_type"]`) access; `dict(obj)` walks both.
        candidate = _to_py_safe(event)
        if not isinstance(candidate, dict):
            try:
                candidate = dict(candidate)
            except Exception:
                return None
        if not isinstance(candidate, dict):
            return None
        for key in _REQUIRED_KEYS:
            if key not in candidate:
                return None
        return {
            "event_type": str(candidate["event_type"]),
            "message_id": str(candidate["message_id"]),
            "timestamp": float(candidate["timestamp"]),
            "received_at": float(candidate["received_at"]),
        }
    except Exception:
        return None


class IndexedDBEventLog:
    """Append-only event log backed by the browser's IndexedDB.

    Provides the same surface as the Pi-side `EventLog` so the
    `MessageSelector` class can consume either backend without change:
    `append(event)` and `query(event_type=None, message_id=None,
    since=None) -> list[dict]`. The cache is also kept in-memory so
    `query()` is O(n) over a bounded list (the cache), not a round-trip
    to IndexedDB per pick.

    `append()` writes to IndexedDB (so the log survives a browser
    restart / tab close) AND refreshes the in-memory cache. `query()`
    reads from the cache only.

    Corrupt events (missing keys, non-numeric timestamps, etc.) are
    silently dropped at cache-build time and at append time — one bad
    entry cannot lose other events.

    Thread-safety: a reentrant lock guards cache mutations and reads.
    IDB is single-threaded in browsers but Python coroutines can race
    in async contexts.

    Args:
        max_entries: Bounded-ring cap on the cache AND on the IDB
            store. Older entries are dropped when the cap is reached.
            Must be a positive integer. Default 100 — matches the
            Pi-side default.
    """

    def __init__(self, max_entries: int = 100) -> None:
        """Open (or create) the IDB database and load the cache."""
        if max_entries < 1:
            raise ValueError(f"max_entries must be a positive integer, got {max_entries}")
        self._max_entries = int(max_entries)
        self._events: list[dict] = []
        self._lock = threading.RLock()
        self._ready = False
        # Lazy IDB init: callers that only construct the object and
        # never query/append (e.g. tests) don't pay the IDB round-trip.
        self._db = None
        self._db_open_failed = False
        self._ensure_db()

    def _ensure_db(self) -> None:
        """Open the IDB database if not already open. No-op when ready."""
        if self._ready or self._db_open_failed:
            return
        try:
            import js  # type: ignore[import-not-found]  # noqa: F401

            req = js.indexedDB.open(_DB_NAME, _DB_VERSION)
            self._wire_db_open(req)
        except Exception as e:
            log.warning("IndexedDBEventLog: failed to open database: %s", e)
            self._db_open_failed = True

    def _wire_db_open(self, req) -> None:
        """Attach `onsuccess` / `onerror` / `onupgradeneeded` to the open request.

        Pyodide's `js.indexedDB.open(...)` returns an `IDBRequest`;
        we wire the success / error / upgrade callbacks via
        `create_proxy` so they can run Python on the JS event loop.
        """

        def _on_success(event):
            try:
                self._db = event.target.result
                self._ready = True
                self._load_cache_from_idb()
            except Exception as e:
                log.warning("IndexedDBEventLog: open success but load failed: %s", e)
                self._db_open_failed = True

        def _on_error(event):
            log.warning("IndexedDBEventLog: open error: %s", event.target.error)
            self._db_open_failed = True

        def _on_upgrade(event):
            db = event.target.result
            if not db.objectStoreNames.contains(_STORE_NAME):
                store = db.createObjectStore(_STORE_NAME, keyPath="message_id")
                # Index on `timestamp` so future filter-by-time queries
                # don't have to scan every key. The selector currently
                # only filters by `message_id` + `event_type`, but the
                # index makes the schema forward-compatible with a
                # future "events since T" debug query.
                store.createIndex("timestamp", "timestamp")

        try:
            from js import create_proxy  # type: ignore[import-not-found]

            req.onsuccess = create_proxy(_on_success)
            req.onerror = create_proxy(_on_error)
            req.onupgradeneeded = create_proxy(_on_upgrade)
        except Exception as e:
            log.warning("IndexedDBEventLog: failed to wire open callbacks: %s", e)
            self._db_open_failed = True

    def _load_cache_from_idb(self) -> None:
        """Populate the in-memory cache from the IDB store. Best-effort."""
        if self._db is None:
            return
        try:
            import js  # type: ignore[import-not-found]  # noqa: F401

            txn = self._db.transaction(_STORE_NAME, "readonly")
            store = txn.objectStore(_STORE_NAME)
            req = store.getAll()
        except Exception as e:
            log.warning("IndexedDBEventLog: getAll failed: %s", e)
            return

        def _on_get_all(event):
            try:
                raw = event.target.result
                if _hasattr_safe(raw, "to_py"):
                    rows = list(_to_py_safe(raw))
                else:
                    rows = []
                    try:
                        iterator = iter(raw)
                    except TypeError:
                        rows = [raw]
                    else:
                        for r in iterator:
                            rows.append(r)
                parsed: list[dict] = []
                for r in rows:
                    cleaned = _clean_event(r)
                    if cleaned is not None:
                        parsed.append(cleaned)
                if len(parsed) > self._max_entries:
                    parsed = parsed[-self._max_entries :]
                with self._lock:
                    self._events = parsed
            except Exception as e:
                log.warning("IndexedDBEventLog: getAll onsuccess failed: %s", e)

        try:
            from js import create_proxy  # type: ignore[import-not-found]

            req.onsuccess = create_proxy(_on_get_all)
        except Exception as e:
            log.warning("IndexedDBEventLog: failed to wire getAll: %s", e)

    # --- Public API (matches heart-matrix-controller/event_log.py) ---

    def append(self, event: dict) -> None:
        """Append one event to the log.

        Persists to IndexedDB AND refreshes the in-memory cache. Drops
        the oldest entry when the cap is reached (FIFO eviction).
        Silently drops events missing the required keys.
        """
        cleaned = _clean_event(event)
        if cleaned is None:
            log.warning("IndexedDBEventLog.append rejected event: %r", event)
            return
        with self._lock:
            self._events.append(cleaned)
            if len(self._events) > self._max_entries:
                self._events = self._events[-self._max_entries :]
        # Best-effort IDB write. If the database hasn't opened yet
        # (e.g. a test that doesn't await), the cache still holds the
        # event — the eventual load will hydrate from whatever did
        # land in IDB.
        self._persist_to_idb(cleaned)

    def _persist_to_idb(self, event: dict) -> None:
        """Write one event to the IDB store. Best-effort, never raises."""
        if not self._ready or self._db is None:
            self._ensure_db()
            return
        try:
            import js  # type: ignore[import-not-found]  # noqa: F401

            txn = self._db.transaction(_STORE_NAME, "readwrite")
            store = txn.objectStore(_STORE_NAME)
            store.put(_to_js_dict(event))
        except Exception as e:
            log.warning("IndexedDBEventLog: put failed: %s", e)

    def query(
        self,
        event_type: str | None = None,
        message_id: str | None = None,
        since: float | None = None,
    ) -> list[dict]:
        """Return events matching the filters, in append order.

        Reads from the in-memory cache (no IDB round-trip). The cache
        is bounded to `max_entries` entries, so the result list is
        always small.

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
        """Re-read the IDB store into the in-memory cache.

        Useful after another tab / window appended events and we want
        to refresh. No-op when the DB isn't ready.
        """
        self._load_cache_from_idb()

    def __len__(self) -> int:
        """Number of events currently in the in-memory cache."""
        with self._lock:
            return len(self._events)

    @property
    def max_entries(self) -> int:
        """The bounded-ring cap."""
        return self._max_entries
