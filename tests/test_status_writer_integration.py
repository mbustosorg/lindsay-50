"""Tests for the unified `StatusWriter.tick()` file + MQTT path.

When the throttle is past, `tick()` writes the `.status.json`
file (existing) AND publishes the MQTT envelope (new) in the
same call. When the throttle is not past, `tick()` is a no-op.

Uses a mock `StatusPublisher`-style callable to assert publish
is invoked with the same dict the file write receives.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from status import StatusSnapshot, StatusWriter


def _snap() -> StatusSnapshot:
    return StatusSnapshot(
        active_sha="abc1234",
        short_sha="abc1234",
        started_at="2026-07-08T10:00:00+00:00",
        updated_at="2026-07-08T10:01:30+00:00",
        uptime_seconds=90,
        mqtt_connected=True,
        last_error=None,
    )


class TestStatusWriterFileAndMqtt:
    def test_tick_writes_file_and_publishes_same_payload(self, tmp_path):
        path = tmp_path / ".status.json"
        builder = MagicMock(return_value=_snap())
        publisher = MagicMock()
        writer = StatusWriter(path, builder, tick_interval_s=0.0, status_publisher=publisher)
        writer.tick()
        # File was written
        assert path.exists()
        payload_on_disk = json.loads(path.read_text())
        # MQTT publisher was called with the same dict
        publisher.assert_called_once()
        published_payload = publisher.call_args[0][0]
        assert published_payload == payload_on_disk
        # The published payload has the 8-key shape
        assert set(published_payload.keys()) == {
            "schema_version",
            "active_sha",
            "short_sha",
            "started_at",
            "updated_at",
            "uptime_seconds",
            "mqtt_connected",
            "last_error",
        }

    def test_tick_is_noop_within_throttle_window(self, tmp_path):
        path = tmp_path / ".status.json"
        builder = MagicMock(return_value=_snap())
        publisher = MagicMock()
        writer = StatusWriter(path, builder, tick_interval_s=10.0, status_publisher=publisher)
        writer.tick()
        assert publisher.call_count == 1
        # Five more ticks within the 10s throttle window: still no-op.
        for _ in range(5):
            writer.tick()
        assert publisher.call_count == 1
        # And the builder was only called the once (within the
        # throttle window, `tick()` is a pure no-op — no builder
        # call, no file write, no publish).
        assert builder.call_count == 1

    def test_tick_publishes_after_throttle_window(self, tmp_path):
        import time

        path = tmp_path / ".status.json"
        builder = MagicMock(return_value=_snap())
        publisher = MagicMock()
        writer = StatusWriter(path, builder, tick_interval_s=0.05, status_publisher=publisher)
        writer.tick()
        time.sleep(0.06)
        writer.tick()
        assert publisher.call_count == 2

    def test_tick_swallows_publisher_exception(self, tmp_path):
        """A publish failure must not prevent the file write or the next cycle."""
        path = tmp_path / ".status.json"
        builder = MagicMock(return_value=_snap())

        def boom(_payload):
            raise RuntimeError("broker down")

        writer = StatusWriter(path, builder, tick_interval_s=0.0, status_publisher=boom)
        # tick() does not raise
        writer.tick()
        # File was still written
        assert path.exists()

    def test_tick_works_without_publisher(self, tmp_path):
        """No publisher configured → file-only mode (backward compat)."""
        path = tmp_path / ".status.json"
        builder = MagicMock(return_value=_snap())
        writer = StatusWriter(path, builder, tick_interval_s=0.0)
        writer.tick()
        assert path.exists()
        payload = json.loads(path.read_text())
        assert payload["active_sha"] == "abc1234"

    def test_tick_skips_when_builder_returns_none(self, tmp_path):
        path = tmp_path / ".status.json"
        builder = MagicMock(return_value=None)
        publisher = MagicMock()
        writer = StatusWriter(path, builder, tick_interval_s=0.0, status_publisher=publisher)
        writer.tick()
        assert not path.exists()
        publisher.assert_not_called()
