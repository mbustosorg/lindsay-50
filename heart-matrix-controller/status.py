"""App-owned `status.json` writer.

The loader validates a staged worktree by spawning `main.py`
briefly, then reading `$REPO_DIR/.status.json`. If the staged
version reports itself healthy (status.json fresh, mqtt connected,
no last_error), the swap goes through.

This module owns the writer side and the snapshot schema. It is
called once per render-loop iteration; the `StatusWriter.tick()`
method is self-throttling (default 5 seconds — unified with the
MQTT publish cadence so a single throttle constant drives both
the file write and the MQTT publish; see Decision 1 in
openspec/changes/archive/2026-07-09-add-sign-status-reports/design.md).

The file uses atomic rename (`os.replace` over a `.tmp` sibling)
so a reader (the loader) never sees a half-written file. The
schema is versioned (`schema_version=1`) so a future breaking
change can be detected.

The same `StatusSnapshot.to_dict()` serializer is the single
serializer for both the `.status.json` file write and the MQTT
wire payload — there is no separate `to_mqtt_dict()` asymmetry
(see Decision 10 in openspec/changes/archive/2026-07-09-add-sign-status-reports/
design.md).

The defensive `read_status()` helper is the loader's read side —
it accepts the same path and returns either a dict-shaped status
or None on any problem (missing file, corrupt JSON, stale
timestamp, missing required keys). The loader never raises on
status reads; it just falls through to "don't swap".
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# Schema version. Bump on any breaking change to the on-disk shape
# so old/new readers can detect "I'm seeing a format I don't know".
SCHEMA_VERSION = 1

# Default throttle interval. SD-card write amplification at 60 Hz
# would wear the card; 5s is the unified cadence for both the
# `.status.json` file write and the MQTT status publish. The
# loader's `BOOT_HOLD_S` was raised from 8s to 17s to match
# (3×5s + 2s slack; see loader.py).
DEFAULT_TICK_INTERVAL_S = 5.0

# Staleness threshold for read_status(): a status.json older than
# this is treated as "the app didn't write it in time" → loader
# rejects the swap.
DEFAULT_STALE_AFTER_S = 10.0


@dataclass
class StatusSnapshot:
    """One snapshot of the app's running state.

    Fields are intentionally simple scalars (no nested dicts, no
    datetime objects) so JSON round-tripping is straightforward
    and the loader can read any field with a single `dict.get`.

    Final shape (Decision 10): schema_version, active_sha, short_sha,
    started_at, updated_at, uptime_seconds (int), mqtt_connected,
    last_error. Three previously-published fields (pid,
    messages_rendered, last_tick_age_ms) have no consumer and were
    dropped across the whole system — both the `.status.json` file
    write and the MQTT wire payload use this same field set.
    """

    schema_version: int = SCHEMA_VERSION
    active_sha: str = ""
    short_sha: str = ""
    started_at: str = ""
    updated_at: str = ""
    uptime_seconds: int = 0
    mqtt_connected: bool = False
    last_error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to a plain dict for JSON serialization.

        This is the SINGLE serializer for both the `.status.json`
        file write and the MQTT wire payload. There is no
        `to_mqtt_dict()` asymmetry — adding it back would force
        per-field keep/drop lists to stay in sync (Decision 10).
        """
        return asdict(self)


class StatusWriter:
    """Throttled, atomic writer for `$REPO_DIR/.status.json`.

    The render loop calls `tick()` every iteration; the writer
    only flushes to disk when at least `tick_interval_s` has
    elapsed since the last write. Errors are swallowed and logged
    — a failed status write must never interrupt rendering.

    The writer is constructed with a path and a snapshot-builder
    callable that returns the live values (so it can read them
    without coupling to the main module). Tests inject a no-op
    builder to drive specific scenarios.

    The `tick()` method also publishes the same snapshot to MQTT
    when an optional `status_publisher` callback is provided.
    The MQTT publish is a second consumer of the same snapshot;
    both happen in the same `tick()` call, at the same throttle
    cadence (Decision 1). `status_publisher` is a callable that
    accepts the dict payload and returns None (it raises on error);
    None means "file only, no MQTT publish" (the default — keeps
    unit tests hermetic without spinning up paho).
    """

    def __init__(
        self,
        path: Path,
        snapshot_builder: Callable[[], StatusSnapshot],
        *,
        tick_interval_s: float = DEFAULT_TICK_INTERVAL_S,
        status_publisher: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> None:
        """Create the writer.

        Args:
            path: Where to write the JSON file. Typically
                `<repo_root>/.status.json`.
            snapshot_builder: Zero-arg callable returning a fresh
                `StatusSnapshot` populated with live values. The
                writer calls this every `tick_interval_s` seconds,
                not every `tick()` call (so it's cheap to call
                `tick()` on the hot path).
            tick_interval_s: How often to flush. Default 5s
                (unified with the MQTT publish cadence).
            status_publisher: Optional callable that publishes the
                serialized payload to MQTT_STATUS_TOPIC. Called
                with the same dict the file write receives. A
                failure must NOT prevent the file write — it is
                logged at WARN and the writer continues.
        """
        self._path = path
        self._snapshot_builder = snapshot_builder
        self._tick_interval_s = tick_interval_s
        self._status_publisher = status_publisher
        self._last_write_monotonic: float = 0.0

    def tick(self) -> None:
        """Called from the render loop on every iteration.

        No-op until `tick_interval_s` has elapsed since the last
        write, at which point it serializes a fresh snapshot and
        renames it into place atomically. When `status_publisher`
        is configured, the same payload is published to MQTT in
        the same call (Decision 1).
        """
        now = time.monotonic()
        if now - self._last_write_monotonic < self._tick_interval_s:
            return
        try:
            snapshot = self._snapshot_builder()
        except Exception as exc:
            logger.warning("status: snapshot_builder raised: %s", exc)
            return
        if snapshot is None:
            return
        payload = snapshot.to_dict()
        if not self._write_atomic(payload):
            return
        # File write succeeded. Publish to MQTT as a side effect; a
        # publish failure must not stop the next throttle cycle (the
        # render loop survives a broker outage).
        if self._status_publisher is not None:
            try:
                self._status_publisher(payload)
            except Exception as exc:
                logger.warning("status: mqtt publish raised: %s", exc)
        self._last_write_monotonic = now

    def _write_atomic(self, payload: dict[str, Any]) -> bool:
        """Write `payload` to `path` atomically.

        Writes to `<path>.tmp`, then `os.replace`s it into place.
        `os.replace` is atomic on POSIX same-filesystem — readers
        see either the old file or the new one, never a half-
        written one.

        Returns True on success, False on any error.
        """
        path = self._path
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, separators=(",", ":"))
            os.replace(tmp, path)
        except Exception as exc:
            logger.warning("status: write failed: %s", exc)
            # Best-effort cleanup; ignore failures (file may not exist).
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            return False
        return True


def make_status_writer(
    *,
    repo_dir: Path,
    snapshot_builder: Callable[[], StatusSnapshot],
    relative_path: str = ".status.json",
    tick_interval_s: float = DEFAULT_TICK_INTERVAL_S,
    status_publisher: Optional[Callable[[dict[str, Any]], None]] = None,
) -> StatusWriter:
    """Create a writer pointed at `<repo_dir>/<relative_path>`.

    Convenience for the standard app case where the status file
    lives at the repo root. Tests inject a tmp dir as `repo_dir`
    rather than reaching in here.
    """
    return StatusWriter(
        path=repo_dir / relative_path,
        snapshot_builder=snapshot_builder,
        tick_interval_s=tick_interval_s,
        status_publisher=status_publisher,
    )


def read_status(
    path: Path,
    *,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
    now_monotonic: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """Defensive read of `$REPO_DIR/.status.json`.

    Returns the parsed dict on success; None on any of:
      - file missing
      - JSON corrupt
      - schema_version mismatch
      - missing required keys
      - file older than `stale_after_s` seconds (per mtime)

    `now_monotonic` defaults to `time.monotonic()`; tests inject a
    fixed value to drive stale-fresh scenarios without sleeping.

    The loader is the only intended caller. It treats None as
    "status.json was not healthy" → don't swap.

    Required keys (post reshape): active_sha (pid was removed — it
    had no consumer). All other keys are present in the dataclass
    default, so the only way a key goes missing is if a future
    snapshot drops it (schema_version mismatch would catch
    that, but the keys tuple is the runtime validator).
    """
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("status: read failed (%s): %s", path, exc)
        return None
    if not isinstance(payload, dict):
        logger.warning("status: not a dict: %r", payload)
        return None
    if payload.get("schema_version") != SCHEMA_VERSION:
        logger.warning("status: schema_version mismatch: %r", payload.get("schema_version"))
        return None
    for required in ("active_sha", "started_at", "updated_at", "mqtt_connected"):
        if required not in payload:
            logger.warning("status: missing required key %r: %r", required, payload)
            return None
    # Staleness check — a status.json that's hours old is the
    # marker of an app that died before the loader could read it.
    # `path.stat().st_mtime` is wall-clock seconds since epoch;
    # compare against `time.time()` (same base). `now_monotonic` is
    # a test override hook — production passes the default.
    now = time.time() if now_monotonic is None else now_monotonic
    age = now - path.stat().st_mtime
    if age > stale_after_s:
        logger.warning("status: stale (age=%.1fs > %.1fs): %s", age, stale_after_s, path)
        return None
    return payload
