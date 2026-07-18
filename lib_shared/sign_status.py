"""Latest status snapshot store (Flask-side subscriber).

The Pi publishes `StatusSnapshot` JSON to MQTT_STATUS_TOPIC every 5s. Flask
subscribes and keeps the most recent payload in `LatestSignStatus` so the
browser can hydrate via `GET /api/sign-status` on page load. The store is a
thin holder: it does NO state computation, NO age tracking, NO TTL
filtering. State and health are browser-side policy; Flask's job is to
keep a copy of the latest payload so a freshly-loaded page can read it.

Thread safety: every read and write passes through a `threading.RLock` so
the paho subscriber thread (which calls `update()`) and Flask's request
thread (which calls `snapshot()`) don't tear the payload. The publisher
calls `update()` from the paho MQTT loop thread; readers call `snapshot()`
and `received_at_wallclock()` from Flask request threads.

Defensive copy semantics: `snapshot()` returns a `dict.copy()` of the
stored payload, so a caller mutating the returned dict cannot corrupt the
in-memory store. This is the same pattern SignConfig uses for runtime
config (`lib_shared.models`).
"""

from __future__ import annotations

import copy
import threading
from datetime import datetime
from typing import Any, Optional

# The 8 keys every StatusSnapshot must carry — see heart-matrix-controller/
# status.py:StatusSnapshot. Both the .status.json file write and the MQTT
# wire payload use the same shape; this constant is the canonical list
# for any consumer that wants to validate against the schema.
# (Decision 10 in openspec/changes/archive/2026-07-09-add-sign-status-reports/design.md.)
REQUIRED_SNAPSHOT_KEYS: tuple[str, ...] = (
    "schema_version",
    "active_sha",
    "short_sha",
    "started_at",
    "updated_at",
    "uptime_seconds",
    "mqtt_connected",
    "last_error",
)


class LatestSignStatus:
    """Thread-safe in-memory holder for the most recent status snapshot.

    Stateful methods (`update`, `snapshot`, `received_at_wallclock`) all
    take the same internal RLock so concurrent reads and writes do not
    tear the payload. The store is reset to empty on construction — the
    Flask subscriber populates it on the first incoming MQTT message.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._snapshot: Optional[dict[str, Any]] = None
        self._received_at: Optional[str] = None

    def update(self, snapshot_dict: dict[str, Any]) -> None:
        """Replace the held snapshot with `snapshot_dict`.

        Validates required keys; raises `ValueError` on any missing key
        without replacing the existing snapshot. The caller (Flask's
        status_dispatch_callback) is responsible for handling the
        ValueError (typically by logging WARN and dropping the payload).
        """
        missing = [k for k in REQUIRED_SNAPSHOT_KEYS if k not in snapshot_dict]
        if missing:
            raise ValueError(f"sign_status.update: missing required keys: {', '.join(missing)}")
        with self._lock:
            # Stash the wall-clock time at update time so the
            # /api/sign-status endpoint can tell the browser when we
            # last received a snapshot — independent of the snapshot's
            # own updated_at (which is the Pi's wall-clock time).
            self._snapshot = copy.deepcopy(snapshot_dict)
            self._received_at = datetime.now().astimezone().isoformat()

    def snapshot(self) -> Optional[dict[str, Any]]:
        """Return a defensive copy of the held snapshot, or None if empty.

        The returned dict is independent of the internal store: mutating
        it does not affect future `snapshot()` calls. Tests use this
        property to verify a returned-dict mutation doesn't corrupt
        state.
        """
        with self._lock:
            if self._snapshot is None:
                return None
            return copy.deepcopy(self._snapshot)

    def received_at_wallclock(self) -> Optional[str]:
        """ISO-8601 wall-clock timestamp at which the most recent update landed.

        Returns None before the first `update()` and the empty string for
        a never-stored snapshot (callers can distinguish via `snapshot()
        is None` rather than checking the string).
        """
        with self._lock:
            return self._received_at
