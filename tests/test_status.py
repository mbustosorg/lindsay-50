"""Tests for `heart-matrix-controller/status.py`.

The status writer is the runtime health surface — the loader
probes staged worktrees by reading the `.status.json` the app
writes. Covers atomic write semantics, throttling, and
defensive read handling.
"""

from __future__ import annotations

import json
import os
import time
from unittest.mock import MagicMock

from status import (
    DEFAULT_TICK_INTERVAL_S,
    SCHEMA_VERSION,
    StatusSnapshot,
    StatusWriter,
    read_status,
)


class TestStatusSnapshot:
    def test_defaults(self):
        snap = StatusSnapshot()
        assert snap.schema_version == SCHEMA_VERSION
        assert snap.pid == 0
        assert snap.active_sha == ""
        assert snap.started_at == ""
        assert snap.updated_at == ""
        assert snap.uptime_seconds == 0.0
        assert snap.mqtt_connected is False
        assert snap.last_tick_age_ms == 0
        assert snap.messages_rendered == 0
        assert snap.last_error is None

    def test_to_dict_round_trip(self):
        snap = StatusSnapshot(
            pid=1234,
            active_sha="abc",
            started_at="2026-07-02T00:00:00+00:00",
            updated_at="2026-07-02T00:01:00+00:00",
            uptime_seconds=60.0,
            mqtt_connected=True,
            last_tick_age_ms=42,
            messages_rendered=10,
            last_error=None,
        )
        d = snap.to_dict()
        assert d["pid"] == 1234
        assert d["active_sha"] == "abc"
        assert d["mqtt_connected"] is True
        # Round-trip via JSON to confirm no non-serializable values.
        json.dumps(d)


class TestStatusWriterAtomicWrites:
    def test_writes_atomically(self, tmp_path):
        path = tmp_path / ".status.json"
        builder = MagicMock(return_value=StatusSnapshot(pid=1234, active_sha="abc"))
        writer = StatusWriter(path, builder, tick_interval_s=0.0)
        writer.tick()
        assert path.exists()
        # No leftover .tmp file.
        assert not (tmp_path / ".status.json.tmp").exists()
        payload = json.loads(path.read_text())
        assert payload["pid"] == 1234
        assert payload["active_sha"] == "abc"
        assert payload["schema_version"] == SCHEMA_VERSION

    def test_swallowed_write_error_does_not_raise(self, tmp_path):
        path = tmp_path / ".status.json"
        builder = MagicMock(return_value=StatusSnapshot(pid=1))
        writer = StatusWriter(path, builder, tick_interval_s=0.0)
        # Replace the writer's path with something unwritable.
        writer._path = tmp_path / "no" / "such" / "dir" / ".status.json"
        # Should not raise.
        writer.tick()

    def test_snapshot_builder_exception_is_swallowed(self, tmp_path):
        path = tmp_path / ".status.json"
        builder = MagicMock(side_effect=RuntimeError("boom"))
        writer = StatusWriter(path, builder, tick_interval_s=0.0)
        writer.tick()
        # No file written, no exception raised.
        assert not path.exists()

    def test_snapshot_builder_returns_none_is_no_op(self, tmp_path):
        path = tmp_path / ".status.json"
        builder = MagicMock(return_value=None)
        writer = StatusWriter(path, builder, tick_interval_s=0.0)
        writer.tick()
        assert not path.exists()


class TestStatusWriterThrottling:
    def test_throttles_repeated_ticks(self, tmp_path):
        path = tmp_path / ".status.json"
        call_count = [0]

        def builder():
            call_count[0] += 1
            return StatusSnapshot(pid=call_count[0])

        writer = StatusWriter(path, builder, tick_interval_s=10.0)
        writer.tick()
        first_count = call_count[0]
        # Five more ticks within the 10s throttle window: should not call builder.
        for _ in range(5):
            writer.tick()
        assert call_count[0] == first_count

    def test_uses_default_interval(self):
        # The constant exists; the writer's default matches.
        assert DEFAULT_TICK_INTERVAL_S == 3.0

    def test_tick_after_interval_flushes(self, tmp_path):
        path = tmp_path / ".status.json"
        builder = MagicMock(return_value=StatusSnapshot(pid=1))
        writer = StatusWriter(path, builder, tick_interval_s=0.05)
        writer.tick()
        time.sleep(0.06)
        writer.tick()
        # Builder called twice across the throttle window.
        assert builder.call_count == 2


class TestReadStatus:
    def test_returns_dict_on_healthy_snapshot(self, tmp_path):
        path = tmp_path / ".status.json"
        snap = StatusSnapshot(pid=42, mqtt_connected=True, last_tick_age_ms=10)
        path.write_text(json.dumps(snap.to_dict()))
        payload = read_status(path, now_monotonic=time.monotonic())
        assert payload is not None
        assert payload["pid"] == 42

    def test_returns_none_when_file_missing(self, tmp_path):
        assert read_status(tmp_path / "missing.json") is None

    def test_returns_none_on_corrupt_json(self, tmp_path):
        path = tmp_path / ".status.json"
        path.write_text("not json {{{")
        assert read_status(path) is None

    def test_returns_none_on_schema_version_mismatch(self, tmp_path):
        path = tmp_path / ".status.json"
        snap = StatusSnapshot(pid=1)
        snap_dict = snap.to_dict()
        snap_dict["schema_version"] = 999
        path.write_text(json.dumps(snap_dict))
        assert read_status(path) is None

    def test_returns_none_when_not_a_dict(self, tmp_path):
        path = tmp_path / ".status.json"
        path.write_text(json.dumps([1, 2, 3]))
        assert read_status(path) is None

    def test_returns_none_when_required_key_missing(self, tmp_path):
        path = tmp_path / ".status.json"
        path.write_text(json.dumps({"schema_version": SCHEMA_VERSION, "pid": 1}))
        assert read_status(path) is None

    def test_returns_none_when_stale(self, tmp_path):
        path = tmp_path / ".status.json"
        snap = StatusSnapshot(pid=1)
        path.write_text(json.dumps(snap.to_dict()))
        # Set mtime to 30s ago — older than the default 10s threshold.
        old_mtime = time.time() - 30
        os.utime(path, (old_mtime, old_mtime))
        assert read_status(path) is None

    def test_freshness_threshold_is_respected(self, tmp_path):
        path = tmp_path / ".status.json"
        snap = StatusSnapshot(pid=1)
        path.write_text(json.dumps(snap.to_dict()))
        # mtime is "now"; with a 10s threshold we should read fine.
        assert read_status(path, now_monotonic=time.monotonic()) is not None

    def test_uses_injected_now_monotonic(self, tmp_path):
        path = tmp_path / ".status.json"
        snap = StatusSnapshot(pid=1)
        path.write_text(json.dumps(snap.to_dict()))
        # Inject a `now_monotonic` 100s past mtime — should be stale.
        now = path.stat().st_mtime + 100
        assert read_status(path, now_monotonic=now) is None
