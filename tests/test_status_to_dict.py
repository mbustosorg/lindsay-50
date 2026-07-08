"""Round-trip safety tests for the `StatusSnapshot.to_dict` serializer.

`to_dict` is the SINGLE serializer for both the `.status.json`
file write and the MQTT wire payload. Any future regression that
re-introduces a dropped field (pid, messages_rendered,
last_tick_age_ms) would re-introduce dead signal — assert
they are absent.

Also asserts `uptime_seconds` is an `int` (truncated, not float).
And the `read_status` change: a payload missing `pid` is
accepted (not rejected); a payload missing `active_sha` is
still rejected.
"""

from __future__ import annotations

import json
import os
import time

from status import StatusSnapshot, read_status


class TestToDictFieldSet:
    def test_field_set_is_exactly_eight_keys(self):
        d = StatusSnapshot().to_dict()
        expected = {
            "schema_version",
            "active_sha",
            "short_sha",
            "started_at",
            "updated_at",
            "uptime_seconds",
            "mqtt_connected",
            "last_error",
        }
        assert set(d.keys()) == expected

    def test_dropped_fields_absent(self):
        """pid, messages_rendered, last_tick_age_ms must not appear in to_dict."""
        d = StatusSnapshot().to_dict()
        assert "pid" not in d
        assert "messages_rendered" not in d
        assert "last_tick_age_ms" not in d

    def test_short_sha_is_in_output(self):
        snap = StatusSnapshot(active_sha="abc1234567890", short_sha="abc1234")
        d = snap.to_dict()
        assert d["short_sha"] == "abc1234"

    def test_uptime_seconds_is_int(self):
        snap = StatusSnapshot(uptime_seconds=90061)
        d = snap.to_dict()
        assert d["uptime_seconds"] == 90061
        assert isinstance(d["uptime_seconds"], int)
        # not a bool (in Python, bool is a subclass of int, so
        # explicitly assert not bool)
        assert not isinstance(d["uptime_seconds"], bool)

    def test_to_dict_round_trip_through_json(self):
        """The wire shape must survive JSON encode → decode unchanged."""
        snap = StatusSnapshot(
            schema_version=1,
            active_sha="abc1234",
            short_sha="abc1234",
            started_at="2026-07-08T10:00:00+00:00",
            updated_at="2026-07-08T10:01:30+00:00",
            uptime_seconds=90,
            mqtt_connected=True,
            last_error=None,
        )
        round_tripped = json.loads(json.dumps(snap.to_dict()))
        # No field went missing on the wire.
        assert set(round_tripped.keys()) == set(snap.to_dict().keys())
        # Values are preserved.
        assert round_tripped["active_sha"] == "abc1234"
        assert round_tripped["uptime_seconds"] == 90
        assert isinstance(round_tripped["uptime_seconds"], int)
        assert round_tripped["mqtt_connected"] is True
        assert round_tripped["last_error"] is None


class TestReadStatusAfterReshape:
    def test_payload_without_pid_is_accepted(self, tmp_path):
        """`pid` is dropped — a payload without pid must read fine."""
        path = tmp_path / ".status.json"
        snap = StatusSnapshot(active_sha="x")
        snap_dict = snap.to_dict()
        assert "pid" not in snap_dict  # sanity
        path.write_text(json.dumps(snap_dict))
        # mtime is "now"
        os.utime(path, (time.time(), time.time()))
        payload = read_status(path, now_monotonic=time.monotonic())
        assert payload is not None
        assert payload["active_sha"] == "x"

    def test_payload_missing_active_sha_is_rejected(self, tmp_path):
        path = tmp_path / ".status.json"
        snap_dict = StatusSnapshot().to_dict()
        del snap_dict["active_sha"]
        path.write_text(json.dumps(snap_dict))
        assert read_status(path) is None

    def test_payload_missing_started_at_is_rejected(self, tmp_path):
        path = tmp_path / ".status.json"
        snap_dict = StatusSnapshot().to_dict()
        del snap_dict["started_at"]
        path.write_text(json.dumps(snap_dict))
        assert read_status(path) is None

    def test_payload_missing_mqtt_connected_is_rejected(self, tmp_path):
        path = tmp_path / ".status.json"
        snap_dict = StatusSnapshot().to_dict()
        del snap_dict["mqtt_connected"]
        path.write_text(json.dumps(snap_dict))
        assert read_status(path) is None

    def test_payload_with_old_pid_field_still_accepted(self, tmp_path):
        """A legacy payload with `pid` is still accepted (the loader
        treats `pid` as an unknown extra — read_status only checks
        for the required keys, not the absence of optional ones).
        """
        path = tmp_path / ".status.json"
        snap_dict = StatusSnapshot().to_dict()
        snap_dict["pid"] = 1234  # legacy field
        path.write_text(json.dumps(snap_dict))
        os.utime(path, (time.time(), time.time()))
        payload = read_status(path, now_monotonic=time.monotonic())
        assert payload is not None
        # The legacy pid is preserved in the returned dict.
        assert payload["pid"] == 1234
