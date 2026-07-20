"""Tests for lib_shared.config_migrations.

Covers:
- MIGRATIONS registry shape
- _v1_to_v2: drops tz_offset_mins, drops rendering, adds v2 blocks, bumps version
- migrate(): no-op for v2 inputs, chains from v1, raises on missing steps
- migrate_on_startup(): fresh install (None), no-op (v2), migration (v1),
  and the writer-call signature
"""

import pytest

from lib_shared.config_migrations import MIGRATIONS, migrate, migrate_on_startup
from lib_shared.models import EffectsSettings, TextSettings

# --- registry / helpers ---


def test_migrations_registry_contains_v1_to_v2():
    """The v1 → v2 migration is registered."""
    assert 1 in MIGRATIONS
    assert callable(MIGRATIONS[1])


def test_migrate_v3_input_is_noop():
    """migrate() on a v3 dict is a no-op (returns input unchanged)."""
    v3 = {
        "version": 3,
        "filters": [],
        "senders": [],
        "effects_settings": EffectsSettings().to_dict(),
        "text_settings": TextSettings().to_dict(),
        "sign_settings": {"sign_name": "S", "timezone": "UTC"},
    }
    out = migrate(v3, current_version=3)
    assert out == v3


def test_migrate_v1_drops_tz_offset_mins():
    """v1 → v3 drops tz_offset_mins."""
    v1 = {
        "version": 1,
        "tz_offset_mins": -420,
        "filters": [],
        "senders": [],
    }
    out = migrate(v1, current_version=3)
    assert "tz_offset_mins" not in out


def test_migrate_v1_drops_rendering():
    """v1 → v3 drops the rendering block."""
    v1 = {
        "version": 1,
        "rendering": {"mode": "scroll", "speed": 0.5, "color": 0xFFFFFF},
        "filters": [],
        "senders": [],
    }
    out = migrate(v1, current_version=3)
    assert "rendering" not in out


def test_migrate_v1_adds_effects_settings():
    """v1 → v3 adds an effects_settings block with canonical defaults."""
    out = migrate({"version": 1, "filters": [], "senders": []}, current_version=3)
    assert "effects_settings" in out
    es = out["effects_settings"]
    assert "effects" in es
    assert es["fade_seconds"] == 2.0


def test_migrate_v1_adds_text_settings():
    """v1 → v3 adds a text_settings block with canonical defaults."""
    out = migrate({"version": 1, "filters": [], "senders": []}, current_version=3)
    assert "text_settings" in out
    ts = out["text_settings"]
    assert ts["speed"] == 3
    assert ts["color"] == 0xFF0000


def test_migrate_v1_bumps_version():
    """v1 → v3 sets version to 3."""
    out = migrate({"version": 1, "filters": [], "senders": []}, current_version=3)
    assert out["version"] == 3


def test_migrate_treats_missing_version_as_v1():
    """A dict without a version key is treated as v1 and migrated to v3."""
    out = migrate({"filters": [], "senders": []}, current_version=3)
    assert out["version"] == 3
    assert "effects_settings" in out
    assert "text_settings" in out
    assert "sign_settings" in out


def test_migrate_preserves_filters():
    """v1 → v3 preserves the filters list (renamed enabled→status)."""
    v1 = {
        "version": 1,
        "filters": [{"type": "keyword", "pattern": "spam", "action": "suppress", "enabled": True}],
        "senders": [],
    }
    out = migrate(v1, current_version=3)
    # The filter is preserved; v1's `enabled: True` migrated to v3's `status: "enabled"`.
    assert len(out["filters"]) == 1
    assert out["filters"][0]["type"] == "keyword"
    assert out["filters"][0]["pattern"] == "spam"


def test_migrate_preserves_senders():
    """v1 → v3 preserves the senders list."""
    v1 = {
        "version": 1,
        "filters": [],
        "senders": [{"phone": "+1", "name": "n"}],
    }
    out = migrate(v1, current_version=3)
    assert len(out["senders"]) == 1
    assert out["senders"][0]["phone"] == "+1"
    assert out["senders"][0]["name"] == "n"


def test_migrate_preserves_sign_and_timezone():
    """v1 → v3 migrates sign+timezone into sign_settings."""
    v1 = {
        "version": 1,
        "sign": {"name": "Old"},
        "timezone": "America/Chicago",
        "filters": [],
        "senders": [],
    }
    out = migrate(v1, current_version=3)
    assert out["sign_settings"]["sign_name"] == "Old"
    assert out["sign_settings"]["timezone"] == "America/Chicago"


def test_migrate_does_not_overwrite_existing_v2_blocks():
    """If the v1 payload already carries the new blocks, those are kept."""
    v1_with_v2 = {
        "version": 1,
        "filters": [],
        "senders": [],
        "effects_settings": {"fade_seconds": 99.0},
        "text_settings": {"color": 0x0000FF},
    }
    out = migrate(v1_with_v2, current_version=3)
    assert out["effects_settings"]["fade_seconds"] == 99.0
    assert out["text_settings"]["color"] == 0x0000FF


def test_migrate_does_not_mutate_input():
    """migrate() returns a new dict; the input is unchanged."""
    v1 = {
        "version": 1,
        "tz_offset_mins": -420,
        "filters": [],
        "senders": [],
    }
    out = migrate(v1, current_version=3)
    assert "tz_offset_mins" in v1
    assert "tz_offset_mins" not in out


def test_migrate_handles_none_input():
    """migrate() with a None input yields an empty dict at the current version."""
    out = migrate(None, current_version=3)
    assert out == {} or "version" in out


def test_migrate_handles_empty_dict():
    """migrate({}) is equivalent to migrate(v1 dict with no fields)."""
    out = migrate({}, current_version=3)
    assert out["version"] == 3
    assert "effects_settings" in out
    assert "text_settings" in out
    assert "sign_settings" in out


def test_migrate_raises_keyerror_for_unknown_version_step():
    """migrate() raises KeyError when a step isn't registered."""
    # MIGRATIONS now has 1→2 and 2→3. Asking for 3→4 has no registered step.
    with pytest.raises(KeyError):
        migrate({"version": 3}, current_version=4)


# --- migrate_on_startup ---


def test_migrate_on_startup_fresh_install_initializes_defaults():
    """When s3_getter returns None, defaults are written via all writers."""
    s3_written = []
    sqlite_written = []
    mqtt_published = []
    log_lines = []

    def s3_getter():
        return None

    def s3_writer(d):
        s3_written.append(d)

    def sqlite_writer(c):
        sqlite_written.append(c)

    def mqtt_publisher(d):
        mqtt_published.append(d)

    out = migrate_on_startup(
        s3_getter=s3_getter,
        sqlite_writer=sqlite_writer,
        mqtt_publisher=mqtt_publisher,
        s3_writer=s3_writer,
        log_func=log_lines.append,
    )
    assert out is not None
    assert out["version"] == 3
    assert len(s3_written) == 1
    assert len(sqlite_written) == 1
    assert len(mqtt_published) == 1


def test_migrate_on_startup_v3_input_is_noop():
    """When s3 already has a v3 config, the writers are NOT called."""
    s3_written = []
    sqlite_written = []
    mqtt_published = []

    def s3_getter():
        return {
            "version": 3,
            "filters": [],
            "senders": [],
            "effects_settings": EffectsSettings().to_dict(),
            "text_settings": TextSettings().to_dict(),
            "sign_settings": {"sign_name": "S", "timezone": "UTC"},
        }

    out = migrate_on_startup(
        s3_getter=s3_getter,
        sqlite_writer=lambda c: sqlite_written.append(c),
        mqtt_publisher=lambda d: mqtt_published.append(d),
        s3_writer=lambda d: s3_written.append(d),
    )
    assert out["version"] == 3
    assert s3_written == []
    assert sqlite_written == []
    assert mqtt_published == []


def test_migrate_on_startup_v1_input_calls_writers():
    """When s3 has a v1 config, the writers receive the migrated v3."""
    s3_written = []
    sqlite_written = []
    mqtt_published = []
    v1 = {
        "version": 1,
        "tz_offset_mins": -420,
        "rendering": {"mode": "scroll", "speed": 0.5, "color": 0xFFFFFF},
        "filters": [{"type": "keyword", "pattern": "spam", "action": "suppress"}],
        "senders": [],
    }

    def s3_getter():
        return v1

    out = migrate_on_startup(
        s3_getter=s3_getter,
        sqlite_writer=lambda c: sqlite_written.append(c),
        mqtt_publisher=lambda d: mqtt_published.append(d),
        s3_writer=lambda d: s3_written.append(d),
    )
    assert out["version"] == 3
    assert "tz_offset_mins" not in out
    assert "rendering" not in out
    assert len(s3_written) == 1
    assert len(sqlite_written) == 1
    assert len(mqtt_published) == 1
    # Filters are preserved through the migration.
    assert sqlite_written[0].filters[0].pattern == "spam"
