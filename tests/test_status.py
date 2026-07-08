"""Tests for `heart-matrix-controller/status.py`.

The status writer is the runtime health surface — the loader
probes staged worktrees by reading the `.status.json` the app
writes. Covers atomic write semantics, throttling, and
defensive read handling.

The snapshot's final field set is the 8-key spec shape:
schema_version, active_sha, short_sha, started_at, updated_at,
uptime_seconds (int), mqtt_connected, last_error. The previous
fields pid / messages_rendered / last_tick_age_ms have no
consumer and were dropped across the whole system.
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
        assert snap.active_sha == ""
        assert snap.short_sha == ""
        assert snap.started_at == ""
        assert snap.updated_at == ""
        assert snap.uptime_seconds == 0
        assert isinstance(snap.uptime_seconds, int)
        assert snap.mqtt_connected is False
        assert snap.last_error is None

    def test_to_dict_round_trip(self):
        snap = StatusSnapshot(
            active_sha="abc1234",
            short_sha="abc1234",
            started_at="2026-07-02T00:00:00+00:00",
            updated_at="2026-07-02T00:01:00+00:00",
            uptime_seconds=60,
            mqtt_connected=True,
            last_error=None,
        )
        d = snap.to_dict()
        # The 8 spec keys are present.
        expected_keys = {
            "schema_version",
            "active_sha",
            "short_sha",
            "started_at",
            "updated_at",
            "uptime_seconds",
            "mqtt_connected",
            "last_error",
        }
        assert set(d.keys()) == expected_keys
        assert d["active_sha"] == "abc1234"
        assert d["short_sha"] == "abc1234"
        assert d["uptime_seconds"] == 60
        assert isinstance(d["uptime_seconds"], int)
        assert d["mqtt_connected"] is True
        # Round-trip via JSON to confirm no non-serializable values.
        json.dumps(d)

    def test_to_dict_drops_pid_messages_rendered_last_tick_age_ms(self):
        """Round-trip safety: the dropped fields must NOT reappear.

        The new 8-key shape replaces pid/messages_rendered/
        last_tick_age_ms. A future regression that re-adds one of
        them would re-introduce dead signal — assert they are
        absent.
        """
        d = StatusSnapshot(active_sha="x").to_dict()
        assert "pid" not in d
        assert "messages_rendered" not in d
        assert "last_tick_age_ms" not in d

    def test_uptime_seconds_is_int_not_float(self):
        """Spec: uptime_seconds is `int` (truncated to whole seconds)."""
        snap = StatusSnapshot(uptime_seconds=90061)
        d = snap.to_dict()
        assert d["uptime_seconds"] == 90061
        assert isinstance(d["uptime_seconds"], int)
        assert not isinstance(d["uptime_seconds"], float)


class TestStatusWriterAtomicWrites:
    def test_writes_atomically(self, tmp_path):
        path = tmp_path / ".status.json"
        builder = MagicMock(return_value=StatusSnapshot(active_sha="abc"))
        writer = StatusWriter(path, builder, tick_interval_s=0.0)
        writer.tick()
        assert path.exists()
        # No leftover .tmp file.
        assert not (tmp_path / ".status.json.tmp").exists()
        payload = json.loads(path.read_text())
        assert payload["active_sha"] == "abc"
        assert payload["schema_version"] == SCHEMA_VERSION

    def test_swallowed_write_error_does_not_raise(self, tmp_path):
        path = tmp_path / ".status.json"
        builder = MagicMock(return_value=StatusSnapshot(active_sha="x"))
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
            return StatusSnapshot(active_sha=str(call_count[0]))

        writer = StatusWriter(path, builder, tick_interval_s=10.0)
        writer.tick()
        first_count = call_count[0]
        # Five more ticks within the 10s throttle window: should not call builder.
        for _ in range(5):
            writer.tick()
        assert call_count[0] == first_count

    def test_uses_default_interval(self):
        # Unified cadence: 5s for both file and MQTT publish.
        assert DEFAULT_TICK_INTERVAL_S == 5.0

    def test_tick_after_interval_flushes(self, tmp_path):
        path = tmp_path / ".status.json"
        builder = MagicMock(return_value=StatusSnapshot(active_sha="x"))
        writer = StatusWriter(path, builder, tick_interval_s=0.05)
        writer.tick()
        time.sleep(0.06)
        writer.tick()
        # Builder called twice across the throttle window.
        assert builder.call_count == 2


class TestReadStatus:
    def test_returns_dict_on_healthy_snapshot(self, tmp_path):
        path = tmp_path / ".status.json"
        snap = StatusSnapshot(active_sha="x", mqtt_connected=True)
        path.write_text(json.dumps(snap.to_dict()))
        payload = read_status(path, now_monotonic=time.monotonic())
        assert payload is not None
        assert payload["active_sha"] == "x"
        assert payload["mqtt_connected"] is True

    def test_accepts_payload_without_pid(self, tmp_path):
        """`pid` is dropped from the snapshot — read_status must not require it."""
        path = tmp_path / ".status.json"
        snap = StatusSnapshot(active_sha="x")
        snap_dict = snap.to_dict()
        assert "pid" not in snap_dict
        path.write_text(json.dumps(snap_dict))
        assert read_status(path, now_monotonic=time.monotonic()) is not None

    def test_returns_none_when_file_missing(self, tmp_path):
        assert read_status(tmp_path / "missing.json") is None

    def test_returns_none_on_corrupt_json(self, tmp_path):
        path = tmp_path / ".status.json"
        path.write_text("not json {{{")
        assert read_status(path) is None

    def test_returns_none_on_schema_version_mismatch(self, tmp_path):
        path = tmp_path / ".status.json"
        snap = StatusSnapshot(active_sha="x")
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
        path.write_text(json.dumps({"schema_version": SCHEMA_VERSION, "active_sha": "x"}))
        # missing started_at, updated_at, mqtt_connected
        assert read_status(path) is None

    def test_returns_none_when_stale(self, tmp_path):
        path = tmp_path / ".status.json"
        snap = StatusSnapshot(active_sha="x")
        path.write_text(json.dumps(snap.to_dict()))
        # Set mtime to 30s ago — older than the default 10s threshold.
        old_mtime = time.time() - 30
        os.utime(path, (old_mtime, old_mtime))
        assert read_status(path) is None

    def test_freshness_threshold_is_respected(self, tmp_path):
        path = tmp_path / ".status.json"
        snap = StatusSnapshot(active_sha="x")
        path.write_text(json.dumps(snap.to_dict()))
        # mtime is "now"; with a 10s threshold we should read fine.
        assert read_status(path, now_monotonic=time.monotonic()) is not None

    def test_uses_injected_now_monotonic(self, tmp_path):
        path = tmp_path / ".status.json"
        snap = StatusSnapshot(active_sha="x")
        path.write_text(json.dumps(snap.to_dict()))
        # Inject a `now_monotonic` 100s past mtime — should be stale.
        now = path.stat().st_mtime + 100
        assert read_status(path, now_monotonic=now) is None
