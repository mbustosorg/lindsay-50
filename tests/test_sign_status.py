"""Tests for `lib_shared/sign_status.py`.

Covers the Flask-side in-memory holder for the most recent StatusSnapshot
received from the Pi over MQTT.

Threads: a threaded test verifies the RLock prevents torn reads under
concurrent `update()` / `snapshot()` calls.
"""

from __future__ import annotations

import threading
from datetime import datetime

import pytest

from lib_shared.sign_status import REQUIRED_SNAPSHOT_KEYS, LatestSignStatus


def _healthy_snapshot() -> dict:
    """Build a fully-populated, spec-compliant snapshot dict for tests."""
    return {
        "schema_version": 2,
        "active_sha": "b5e191c5df481d51c4e7d1cced51cf7c656f1ead",
        "short_sha": "b5e191c",
        "started_at": "2026-07-08T10:00:00+00:00",
        "updated_at": "2026-07-08T10:01:30+00:00",
        "uptime_seconds": 90,
        "mqtt_connected": True,
        "last_error": None,
        # issue #71 — v2 schema gain.
        "applied_config_sha": "abc1234",
    }


def test_required_snapshot_keys_constant_lists_nine_keys():
    """Schema-versioned field set must contain exactly the 9 spec keys.

    v2 (issue #71) adds `applied_config_sha` to the validator's
    required-keys list so a v1 Pi binary that writes an 8-key payload
    is rejected by the loader's read_status() probe (the right
    behavior — auto-upgrade pushes the new binary in <30s).
    """
    expected = {
        "schema_version",
        "active_sha",
        "short_sha",
        "started_at",
        "updated_at",
        "uptime_seconds",
        "mqtt_connected",
        "last_error",
        "applied_config_sha",
    }
    assert set(REQUIRED_SNAPSHOT_KEYS) == expected
    assert len(REQUIRED_SNAPSHOT_KEYS) == 9


class TestLatestSignStatusBasics:
    def test_snapshot_returns_none_when_empty(self):
        store = LatestSignStatus()
        assert store.snapshot() is None

    def test_received_at_returns_none_when_empty(self):
        store = LatestSignStatus()
        assert store.received_at_wallclock() is None

    def test_update_stores_snapshot(self):
        store = LatestSignStatus()
        snap = _healthy_snapshot()
        store.update(snap)
        out = store.snapshot()
        assert out is not None
        assert out["active_sha"] == snap["active_sha"]
        assert out["uptime_seconds"] == 90
        assert out["mqtt_connected"] is True

    def test_received_at_is_iso8601_after_update(self):
        store = LatestSignStatus()
        store.update(_healthy_snapshot())
        ts = store.received_at_wallclock()
        assert ts is not None
        # datetime.fromisoformat is the canonical ISO-8601 parser; if it
        # accepts the string, the format is well-formed.
        parsed = datetime.fromisoformat(ts)
        assert isinstance(parsed, datetime)


class TestDefensiveCopy:
    def test_snapshot_returns_defensive_copy(self):
        """Mutating the returned dict must NOT affect the next snapshot()."""
        store = LatestSignStatus()
        store.update(_healthy_snapshot())
        first = store.snapshot()
        assert first is not None
        first["mqtt_connected"] = "MUTATED"
        first["uptime_seconds"] = -1
        # Re-read — must be the original values, not our mutations.
        second = store.snapshot()
        assert second is not None
        assert second["mqtt_connected"] is True
        assert second["uptime_seconds"] == 90

    def test_snapshot_is_nested_safe(self):
        """A nested-mutating caller must not corrupt the store."""
        store = LatestSignStatus()
        snap = _healthy_snapshot()
        # Inject a nested dict — not part of the spec, but defensive copy
        # must hold even for arbitrary shapes we don't currently emit.
        snap["meta"] = {"nested": {"value": 1}}
        store.update(snap)
        first = store.snapshot()
        assert first is not None
        first["meta"]["nested"]["value"] = 999
        second = store.snapshot()
        assert second is not None
        assert second["meta"]["nested"]["value"] == 1


class TestRejection:
    def test_update_raises_on_missing_required_key(self):
        store = LatestSignStatus()
        bad = _healthy_snapshot()
        del bad["active_sha"]
        with pytest.raises(ValueError) as excinfo:
            store.update(bad)
        assert "active_sha" in str(excinfo.value)

    def test_update_does_not_replace_store_on_missing_key(self):
        """A rejected update must leave the existing snapshot intact."""
        store = LatestSignStatus()
        good = _healthy_snapshot()
        store.update(good)
        bad = _healthy_snapshot()
        del bad["last_error"]
        with pytest.raises(ValueError):
            store.update(bad)
        out = store.snapshot()
        assert out is not None
        assert out["last_error"] is None  # the original good value, unchanged

    def test_update_accepts_empty_last_error_string(self):
        """last_error='' is valid (treated as no error by the browser)."""
        store = LatestSignStatus()
        snap = _healthy_snapshot()
        snap["last_error"] = ""
        store.update(snap)  # must not raise
        out = store.snapshot()
        assert out is not None
        assert out["last_error"] == ""


class TestReplaceOnlyLatest:
    def test_three_updates_only_last_is_returned(self):
        """The store keeps only the most recent payload, not a history."""
        store = LatestSignStatus()
        first = _healthy_snapshot()
        first["updated_at"] = "2026-07-08T10:00:00+00:00"
        store.update(first)
        second = _healthy_snapshot()
        second["updated_at"] = "2026-07-08T10:00:05+00:00"
        second["uptime_seconds"] = 95
        store.update(second)
        third = _healthy_snapshot()
        third["updated_at"] = "2026-07-08T10:00:10+00:00"
        third["uptime_seconds"] = 100
        store.update(third)
        out = store.snapshot()
        assert out is not None
        assert out["updated_at"] == "2026-07-08T10:00:10+00:00"
        assert out["uptime_seconds"] == 100


class TestReceivedAtReplacement:
    def test_received_at_advances_on_each_update(self):
        """Each update must produce a received_at >= previous (wall clock)."""
        store = LatestSignStatus()
        store.update(_healthy_snapshot())
        ts1 = store.received_at_wallclock()
        # Brief gap so datetime.now() increments at least the microseconds.
        store.update(_healthy_snapshot())
        ts2 = store.received_at_wallclock()
        assert ts1 is not None and ts2 is not None
        assert datetime.fromisoformat(ts2) >= datetime.fromisoformat(ts1)


class TestConcurrentUpdates:
    def test_concurrent_updates_do_not_corrupt_store(self):
        """Two threads updating simultaneously — the lock prevents torn writes.

        With the lock, the final state is one of the submitted payloads
        (whichever thread won the race last). Without the lock, we'd see
        a half-updated dict (missing some keys, with others from a
        different snapshot).
        """
        store = LatestSignStatus()
        errors: list[Exception] = []
        iterations = 50

        def updater(label: str) -> None:
            try:
                for i in range(iterations):
                    snap = _healthy_snapshot()
                    snap["active_sha"] = f"{label}-{i}"
                    store.update(snap)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=updater, args=("T1",))
        t2 = threading.Thread(target=updater, args=("T2",))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        assert not errors
        # Final state must be one of the two writes' payloads — i.e.
        # complete (all required keys) and an `active_sha` from one
        # of the writers.
        out = store.snapshot()
        assert out is not None
        for key in REQUIRED_SNAPSHOT_KEYS:
            assert key in out
        prefix = out["active_sha"].split("-")[0]
        assert prefix in ("T1", "T2")

    def test_concurrent_reader_and_writer_see_consistent_state(self):
        """A reader (snapshot()) and a writer (update()) running in parallel
        must always see either the pre-write or post-write state, never a
        torn dict that's missing required keys."""
        store = LatestSignStatus()
        store.update(_healthy_snapshot())  # seed so reader doesn't see None
        errors: list[Exception] = []
        stop = threading.Event()

        def writer() -> None:
            try:
                for i in range(50):
                    snap = _healthy_snapshot()
                    snap["active_sha"] = f"writer-{i}"
                    store.update(snap)
            except Exception as exc:
                errors.append(exc)

        def reader() -> None:
            try:
                for _ in range(200):
                    if stop.is_set():
                        break
                    out = store.snapshot()
                    if out is not None:
                        for key in REQUIRED_SNAPSHOT_KEYS:
                            assert key in out, f"torn read: missing {key}"
            except Exception as exc:
                errors.append(exc)

        w = threading.Thread(target=writer)
        r = threading.Thread(target=reader)
        w.start()
        r.start()
        w.join(timeout=5)
        stop.set()
        r.join(timeout=5)
        assert not errors


class TestSourceTracking:
    """Issue #71: track whether the held snapshot came from a live
    WS message or was re-hydrated from the persisted SQLite row on
    Flask startup. Defaults to "live" on update(); explicit "persisted"
    on the startup-restore path. Flips back to "live" automatically on
    the next update() (whether explicit source or default)."""

    def test_source_returns_empty_string_when_empty(self):
        store = LatestSignStatus()
        assert store.source() == ""

    def test_default_update_records_source_live(self):
        store = LatestSignStatus()
        store.update(_healthy_snapshot())
        assert store.source() == "live"

    def test_explicit_source_persisted_recorded(self):
        store = LatestSignStatus()
        store.update(_healthy_snapshot(), source="persisted")
        assert store.source() == "persisted"

    def test_subsequent_live_update_overrides_persisted(self):
        """The startup-restore tag must be replaced by the first live
        WS message that arrives later — operator should not see the
        amber "Last seen T ago" badge forever."""
        store = LatestSignStatus()
        store.update(_healthy_snapshot(), source="persisted")
        assert store.source() == "persisted"
        store.update(_healthy_snapshot())
        assert store.source() == "live"

    def test_rejected_update_does_not_change_source(self):
        """A malformed payload that fails validation must leave the
        existing source untouched (the WS path drops the payload
        anyway, but we don't want a half-applied state)."""
        store = LatestSignStatus()
        store.update(_healthy_snapshot(), source="persisted")
        assert store.source() == "persisted"
        bad = _healthy_snapshot()
        del bad["applied_config_sha"]
        with pytest.raises(ValueError):
            store.update(bad)
        assert store.source() == "persisted"
