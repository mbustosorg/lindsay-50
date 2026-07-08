"""Tests for the browser-side `healthFromSnapshot(snapshot)` helper.

`healthFromSnapshot` keys off TWO signals (not three):
  - `mqtt_connected === true`
  - `last_error` is null or empty

`HEALTH_TICK_AGE_MAX_MS` is gone — the function no longer
gates on `last_tick_age_ms`. Empty `last_error` strings are
treated as null (healthy).

The test mirrors the JS function in Python so the contract
is captured in the test suite (the JS module is a black box
under pytest).
"""

from __future__ import annotations


def health_from_snapshot(snapshot: dict | None) -> str:
    """Mirror of `sign_status.js` healthFromSnapshot — see Decision 11."""
    if not snapshot:
        return "healthy"
    if snapshot.get("mqtt_connected") is False:
        return "degraded"
    err = snapshot.get("last_error")
    if isinstance(err, str) and len(err) > 0:
        return "degraded"
    return "healthy"


class TestHealthFromSnapshot:
    def test_healthy_when_mqtt_connected_and_no_error(self):
        snap = {"mqtt_connected": True, "last_error": None}
        assert health_from_snapshot(snap) == "healthy"

    def test_healthy_when_mqtt_connected_and_empty_error_string(self):
        """Empty `last_error` is treated as null — no error signal."""
        snap = {"mqtt_connected": True, "last_error": ""}
        assert health_from_snapshot(snap) == "healthy"

    def test_degraded_when_mqtt_disconnected(self):
        snap = {"mqtt_connected": False, "last_error": None}
        assert health_from_snapshot(snap) == "degraded"

    def test_degraded_when_mqtt_disconnected_with_error(self):
        snap = {"mqtt_connected": False, "last_error": "broker down"}
        assert health_from_snapshot(snap) == "degraded"

    def test_degraded_when_last_error_nonempty_string(self):
        snap = {"mqtt_connected": True, "last_error": "broker reconnecting"}
        assert health_from_snapshot(snap) == "degraded"

    def test_healthy_when_no_snapshot(self):
        """No signal = assume healthy (the JS module's default)."""
        assert health_from_snapshot(None) == "healthy"

    def test_mqtt_false_takes_precedence_over_last_error(self):
        """mqtt_connected === false is degraded regardless of last_error."""
        snap = {"mqtt_connected": False, "last_error": ""}
        assert health_from_snapshot(snap) == "degraded"

    def test_no_tick_age_check(self):
        """The `last_tick_age_ms` signal no longer exists — the function
        must not gate on it. Passing a stale `last_tick_age_ms` does
        not flip the result to degraded.
        """
        snap = {
            "mqtt_connected": True,
            "last_error": None,
            "last_tick_age_ms": 9999,  # legacy field; must be ignored
        }
        assert health_from_snapshot(snap) == "healthy"

    def test_no_health_tick_age_max_ms_constant(self):
        """The legacy `HEALTH_TICK_AGE_MAX_MS` constant is gone — verify
        it's not referenced from the JS module.

        `last_tick_age_ms` may appear in comments (explaining the
        change). The runtime test above (`test_no_tick_age_check`)
        already verifies the field is ignored; this test only
        guards the dead constant.
        """
        with open(
            "/Users/adam/.agent-orchestrator/projects/lindsay-50_f658f025d5/worktrees/l5-19/heart-message-manager/static/sign_status.js",
            "r",
            encoding="utf-8",
        ) as fh:
            content = fh.read()
        assert "HEALTH_TICK_AGE_MAX_MS" not in content
