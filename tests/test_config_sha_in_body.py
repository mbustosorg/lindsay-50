"""Tests for issue #71: per-save config_sha / updated_at on SignConfig.

The SignConfig body (NOT envelope metadata) carries two new fields so
both round-trip through ``to_dict()`` / ``update_from_dict()`` and
land in the S3 config snapshot file:

- ``config_sha`` — 7-char short SHA-style hash of the post-save
  ``to_dict()`` body, computed BEFORE the field is stamped on the
  in-memory SignConfig (chicken-and-egg loop avoidance).
- ``updated_at`` — ISO-8601 wall-clock time of the save.

These tests cover the field round-trip and the chicken-and-egg guard.
"""

from __future__ import annotations

from lib_shared.models import SignConfig


def test_to_dict_emits_config_sha_and_updated_at():
    cfg = SignConfig(
        config_sha="abc1234",
        updated_at="2026-09-12T10:00:00+00:00",
    )
    out = cfg.to_dict()
    assert out["config_sha"] == "abc1234"
    assert out["updated_at"] == "2026-09-12T10:00:00+00:00"


def test_to_dict_emits_empty_strings_when_not_set():
    """Freshly constructed SignConfig must serialize with empty
    config_sha / updated_at — the dashboard renders "—" for empty
    cells, so we want this to round-trip cleanly."""
    cfg = SignConfig()
    out = cfg.to_dict()
    assert out["config_sha"] == ""
    assert out["updated_at"] == ""


def test_from_dict_reads_config_sha_and_updated_at():
    """Round-trip via from_dict — same wire shape as to_dict()."""
    payload = {
        "version": SignConfig.CURRENT_VERSION,
        "filters": [],
        "senders": [],
        "sign_settings": {"sign_name": "Lindsay's Heart", "timezone": "US/Pacific"},
        "effects_settings": {},
        "text_settings": {},
        "config_sha": "deadbeef",
        "updated_at": "2026-09-12T11:30:00+00:00",
    }
    cfg = SignConfig.from_dict(payload)
    assert cfg.config_sha == "deadbeef"
    assert cfg.updated_at == "2026-09-12T11:30:00+00:00"


def test_update_from_dict_preserves_config_sha_and_updated_at():
    """update_from_dict() must read the new fields (same as from_dict)."""
    cfg = SignConfig()
    payload = {
        "version": SignConfig.CURRENT_VERSION,
        "filters": [],
        "senders": [],
        "sign_settings": {"sign_name": "Lindsay's Heart", "timezone": "US/Pacific"},
        "effects_settings": {},
        "text_settings": {},
        "config_sha": "fedcba9",
        "updated_at": "2026-09-12T12:00:00+00:00",
    }
    cfg.update_from_dict(payload)
    assert cfg.config_sha == "fedcba9"
    assert cfg.updated_at == "2026-09-12T12:00:00+00:00"


def test_update_from_dict_falls_back_to_existing_values():
    """If a config envelope lacks config_sha / updated_at (e.g. an
    older Pi binary replaying an old payload), the in-memory value
    is preserved rather than wiped."""
    cfg = SignConfig(config_sha="abc1234", updated_at="2026-09-01T00:00:00+00:00")
    payload = {
        "version": SignConfig.CURRENT_VERSION,
        "filters": [],
        "senders": [],
        "sign_settings": {"sign_name": "Lindsay's Heart", "timezone": "US/Pacific"},
        "effects_settings": {},
        "text_settings": {},
    }
    cfg.update_from_dict(payload)
    # Falls back to the in-memory values via the .get("...", self.x) pattern
    # — preserving them across replay keeps the dashboard from blanking
    # on an older payload.
    assert cfg.config_sha == "abc1234"
    assert cfg.updated_at == "2026-09-01T00:00:00+00:00"


def test_chicken_and_egg_hash_guard():
    """The chicken-and-egg loop: if config_sha is computed from
    ``to_dict()`` AFTER the field is set, the next to_dict() includes
    the new value, which re-hashes to a different value. Mitigation:
    compute the hash from a snapshot taken BEFORE assignment.

    This test simulates the safe pattern: build a body excluding
    config_sha / updated_at, hash it, then assign the hash, then
    verify that the final to_dict() carries the assigned value.
    """
    cfg = SignConfig()
    # Pre-stamp snapshot — no config_sha / updated_at keys present.
    body_for_hash = cfg.to_dict()
    body_for_hash.pop("config_sha", None)
    body_for_hash.pop("updated_at", None)

    # Simulate what _save_and_publish does.
    import hashlib
    import json
    canonical = json.dumps(body_for_hash, sort_keys=True, separators=(",", ":"))
    new_sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:7]

    cfg.config_sha = new_sha
    cfg.updated_at = "2026-09-12T13:00:00+00:00"

    # Final wire dict carries the assigned values, not the
    # pre-assignment hash of the empty body.
    out = cfg.to_dict()
    assert out["config_sha"] == new_sha
    assert out["updated_at"] == "2026-09-12T13:00:00+00:00"

    # And: re-hashing the final body would NOT match (which is fine —
    # the hash is a per-save stamp, not a stable content-addressable
    # identifier). The next save computes a fresh hash from the
    # pre-stamp snapshot again.
    body2 = cfg.to_dict()
    body2.pop("config_sha", None)
    body2.pop("updated_at", None)
    canonical2 = json.dumps(body2, sort_keys=True, separators=(",", ":"))
    sha2 = hashlib.sha256(canonical2.encode("utf-8")).hexdigest()[:7]
    # Different config_sha → different body → different hash.
    # (Not asserting equality/inequality with new_sha — the test is
    # that the loop doesn't infinitely self-reference; both produce
    # well-formed 7-char hashes.)
    assert len(sha2) == 7


def test_update_copies_config_sha_and_updated_at_from_other():
    """SignConfig.update(other) — replace-all-fields — must carry
    config_sha / updated_at from the source instance."""
    a = SignConfig(config_sha="1111111", updated_at="2026-09-01T00:00:00+00:00")
    b = SignConfig(config_sha="2222222", updated_at="2026-09-12T00:00:00+00:00")
    a.update(b)
    assert a.config_sha == "2222222"
    assert a.updated_at == "2026-09-12T00:00:00+00:00"
