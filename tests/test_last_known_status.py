"""Tests for issue #71: Pi status persistence in sign_status_log.

On Flask startup, the most-recent validated ``StatusSnapshot`` payload
is read from SQLite and fed back into the same
``LatestSignStatus.update()`` code path that MQTT messages use — so
the dashboard pill comes up pre-populated.

The SQLite table is append-only (``put_last_status_payload`` always
inserts; the table is shaped for a future ring-buffer trim). The
read-side returns the most-recent row by ``received_at`` DESC.

These tests run against a tempdir-managed SQLite file via a
``monkeypatch`` of ``sqlite._db_path``.
"""

from __future__ import annotations

import json

import pytest


HEALTHY_SNAPSHOT = {
    "schema_version": 2,
    "active_sha": "b5e191c5df481d51c4e7d1cced51cf7c656f1ead",
    "short_sha": "b5e191c",
    "started_at": "2026-09-12T10:00:00+00:00",
    "updated_at": "2026-09-12T10:01:30+00:00",
    "uptime_seconds": 90,
    "mqtt_connected": True,
    "last_error": None,
    "applied_config_sha": "abc1234",
}


@pytest.fixture
def sqlite_mod(tmp_path, monkeypatch):
    """Return the sqlite module with ``_db_path`` redirected to a
    per-test file. ``init_db()`` runs against the tempdir so each
    test gets a fresh schema."""
    import heart_message_manager.sqlite as sql_mod

    db_file = tmp_path / "test_status_log.sqlite"
    monkeypatch.setattr(sql_mod, "_db_path", lambda: db_file)
    sql_mod.init_db()
    yield sql_mod


def test_put_last_status_payload_appends_row(sqlite_mod):
    """A fresh insert lands in the table as a new row (autoincrement id)."""
    payload_json = json.dumps(HEALTHY_SNAPSHOT)
    sqlite_mod.put_last_status_payload(payload_json, "2026-09-12T10:01:30+00:00")
    out = sqlite_mod.get_latest_status_payload()
    assert out is not None
    received_at, stored_json = out
    assert received_at == "2026-09-12T10:01:30+00:00"
    assert json.loads(stored_json) == HEALTHY_SNAPSHOT


def test_put_last_status_payload_does_not_overwrite(sqlite_mod):
    """Every insert adds a new row — the table is append-only."""
    sqlite_mod.put_last_status_payload(
        json.dumps(HEALTHY_SNAPSHOT), "2026-09-12T10:00:00+00:00"
    )
    second = dict(HEALTHY_SNAPSHOT, uptime_seconds=120)
    sqlite_mod.put_last_status_payload(
        json.dumps(second), "2026-09-12T10:01:00+00:00"
    )
    sqlite_mod.put_last_status_payload(
        json.dumps(HEALTHY_SNAPSHOT), "2026-09-12T10:02:00+00:00"
    )
    # All three rows must be present (the table is shaped for future
    # ring-buffer trimming; the current code does not trim).
    out = sqlite_mod.get_latest_status_payload()
    assert out is not None
    received_at = out[0]
    assert received_at == "2026-09-12T10:02:00+00:00"


def test_get_latest_status_payload_returns_most_recent(sqlite_mod):
    """Tie-breaker: when two payloads share the same received_at,
    the higher autoincrement id wins (more recent insert)."""
    sqlite_mod.put_last_status_payload(
        json.dumps(HEALTHY_SNAPSHOT), "2026-09-12T10:00:00+00:00"
    )
    newer = dict(HEALTHY_SNAPSHOT, uptime_seconds=200)
    sqlite_mod.put_last_status_payload(
        json.dumps(newer), "2026-09-12T10:01:00+00:00"
    )
    sqlite_mod.put_last_status_payload(
        json.dumps(HEALTHY_SNAPSHOT), "2026-09-12T10:00:00+00:00"  # older
    )
    out = sqlite_mod.get_latest_status_payload()
    assert out is not None
    received_at, stored_json = out
    assert received_at == "2026-09-12T10:01:00+00:00"
    assert json.loads(stored_json)["uptime_seconds"] == 200


def test_get_latest_status_payload_returns_none_when_empty(sqlite_mod):
    """Fresh DB with no rows → None (caller short-circuits the
    startup restore)."""
    out = sqlite_mod.get_latest_status_payload()
    assert out is None


def test_startup_restore_via_latest_sign_status_update(sqlite_mod):
    """The startup restore reads the persisted JSON and feeds it
    through the SAME validator the WS path uses (LatestSignStatus.update).
    This means a corrupted persisted row is rejected by the same
    validator and the startup restore short-circuits cleanly — never
    crashes the boot path."""
    from lib_shared.sign_status import LatestSignStatus

    sqlite_mod.put_last_status_payload(
        json.dumps(HEALTHY_SNAPSHOT), "2026-09-12T10:01:30+00:00"
    )
    out = sqlite_mod.get_latest_status_payload()
    assert out is not None
    _, payload_json = out
    parsed = json.loads(payload_json)

    store = LatestSignStatus()
    # source="persisted" — distinguishes startup-restore from live WS.
    store.update(parsed, source="persisted")
    snap = store.snapshot()
    assert snap is not None
    assert snap["applied_config_sha"] == "abc1234"
    assert snap["short_sha"] == "b5e191c"
    assert store.source() == "persisted"


def test_startup_restore_rejects_corrupted_persisted_row(sqlite_mod):
    """A persisted row missing a required key (e.g. an older binary
    wrote a v1 snapshot before the v2 schema bump) must NOT crash
    the startup path — the validator raises, the caller catches,
    the store stays empty, and the dashboard shows the empty-state
    pill."""
    from lib_shared.sign_status import LatestSignStatus

    bad_payload = dict(HEALTHY_SNAPSHOT)
    del bad_payload["applied_config_sha"]  # v1 schema, no applied_config_sha
    sqlite_mod.put_last_status_payload(
        json.dumps(bad_payload), "2026-09-12T10:01:30+00:00"
    )

    out = sqlite_mod.get_latest_status_payload()
    assert out is not None
    _, payload_json = out
    parsed = json.loads(payload_json)

    store = LatestSignStatus()
    with pytest.raises(ValueError):
        store.update(parsed)  # validator rejects
    # Store stays empty.
    assert store.snapshot() is None
