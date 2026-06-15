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


def test_migrate_v2_input_is_noop():
    """migrate() on a v2 dict is a no-op (returns input unchanged)."""
    v2 = {
        "version": 2,
        "filters": [],
        "senders": [],
        "effect_settings": EffectsSettings().to_dict(),
        "text_settings": TextSettings().to_dict(),
    }
    out = migrate(v2, current_version=2)
    assert out == v2


def test_migrate_v1_drops_tz_offset_mins():
    """v1 → v2 drops tz_offset_mins."""
    v1 = {
        "version": 1,
        "tz_offset_mins": -420,
        "filters": [],
        "senders": [],
    }
    out = migrate(v1, current_version=2)
    assert "tz_offset_mins" not in out


def test_migrate_v1_drops_rendering():
    """v1 → v2 drops the rendering block."""
    v1 = {
        "version": 1,
        "rendering": {"mode": "scroll", "speed": 0.5, "color": 0xFFFFFF},
        "filters": [],
        "senders": [],
    }
    out = migrate(v1, current_version=2)
    assert "rendering" not in out


def test_migrate_v1_adds_effect_settings():
    """v1 → v2 adds an effect_settings block with canonical defaults."""
    out = migrate({"version": 1, "filters": [], "senders": []}, current_version=2)
    assert "effect_settings" in out
    es = out["effect_settings"]
    assert "effects" in es
    assert es["fade_seconds"] == 2.0
    assert es["hold_seconds"] == 15.0


def test_migrate_v1_adds_text_settings():
    """v1 → v2 adds a text_settings block with canonical defaults."""
    out = migrate({"version": 1, "filters": [], "senders": []}, current_version=2)
    assert "text_settings" in out
    ts = out["text_settings"]
    assert ts["frame_delay"] == 0.04
    assert ts["color"] == 0xFF0000


def test_migrate_v1_bumps_version():
    """v1 → v2 sets version to 2."""
    out = migrate({"version": 1, "filters": [], "senders": []}, current_version=2)
    assert out["version"] == 2


def test_migrate_treats_missing_version_as_v1():
    """A dict without a version key is treated as v1 and migrated to v2."""
    out = migrate({"filters": [], "senders": []}, current_version=2)
    assert out["version"] == 2
    assert "effect_settings" in out
    assert "text_settings" in out


def test_migrate_preserves_filters():
    """v1 → v2 preserves the filters list unchanged."""
    v1 = {
        "version": 1,
        "filters": [{"type": "keyword", "pattern": "spam", "action": "suppress"}],
        "senders": [],
    }
    out = migrate(v1, current_version=2)
    assert out["filters"] == v1["filters"]


def test_migrate_preserves_senders():
    """v1 → v2 preserves the senders list unchanged."""
    v1 = {
        "version": 1,
        "filters": [],
        "senders": [{"phone": "+1", "name": "n"}],
    }
    out = migrate(v1, current_version=2)
    assert out["senders"] == v1["senders"]


def test_migrate_preserves_sign_and_timezone():
    """v1 → v2 preserves sign and timezone."""
    v1 = {
        "version": 1,
        "sign": {"name": "Old"},
        "timezone": "America/Chicago",
        "filters": [],
        "senders": [],
    }
    out = migrate(v1, current_version=2)
    assert out["sign"] == v1["sign"]
    assert out["timezone"] == v1["timezone"]


def test_migrate_does_not_overwrite_existing_v2_blocks():
    """If the v1 payload already carries the new blocks, those are kept."""
    v1_with_v2 = {
        "version": 1,
        "filters": [],
        "senders": [],
        "effect_settings": {"fade_seconds": 99.0},
        "text_settings": {"color": 0x0000FF},
    }
    out = migrate(v1_with_v2, current_version=2)
    assert out["effect_settings"]["fade_seconds"] == 99.0
    assert out["text_settings"]["color"] == 0x0000FF


def test_migrate_does_not_mutate_input():
    """migrate() returns a new dict; the input is unchanged."""
    v1 = {
        "version": 1,
        "tz_offset_mins": -420,
        "filters": [],
        "senders": [],
    }
    out = migrate(v1, current_version=2)
    assert "tz_offset_mins" in v1
    assert "tz_offset_mins" not in out


def test_migrate_handles_none_input():
    """migrate() with a None input yields an empty dict at the current version."""
    out = migrate(None, current_version=2)
    assert out == {} or "version" in out


def test_migrate_handles_empty_dict():
    """migrate({}) is equivalent to migrate(v1 dict with no fields)."""
    out = migrate({}, current_version=2)
    assert out["version"] == 2
    assert "effect_settings" in out
    assert "text_settings" in out


def test_migrate_raises_keyerror_for_unknown_version_step():
    """migrate() raises KeyError when a step isn't registered."""
    # The registry only has v1 → v2; ask for v1 → v3 and expect an error.
    with pytest.raises(KeyError):
        migrate({"version": 1}, current_version=3)


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
    assert out["version"] == 2
    assert len(s3_written) == 1
    assert len(sqlite_written) == 1
    assert len(mqtt_published) == 1


def test_migrate_on_startup_v2_input_is_noop():
    """When s3 already has a v2 config, the writers are NOT called."""
    s3_written = []
    sqlite_written = []
    mqtt_published = []

    def s3_getter():
        return {
            "version": 2,
            "filters": [],
            "senders": [],
            "effect_settings": EffectsSettings().to_dict(),
            "text_settings": TextSettings().to_dict(),
        }

    out = migrate_on_startup(
        s3_getter=s3_getter,
        sqlite_writer=lambda c: sqlite_written.append(c),
        mqtt_publisher=lambda d: mqtt_published.append(d),
        s3_writer=lambda d: s3_written.append(d),
    )
    assert out["version"] == 2
    assert s3_written == []
    assert sqlite_written == []
    assert mqtt_published == []


def test_migrate_on_startup_v1_input_calls_writers():
    """When s3 has a v1 config, the writers receive the migrated v2."""
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
    assert out["version"] == 2
    assert "tz_offset_mins" not in out
    assert "rendering" not in out
    assert len(s3_written) == 1
    assert len(sqlite_written) == 1
    assert len(mqtt_published) == 1
    # Filters are preserved through the migration.
    assert sqlite_written[0].filters[0].pattern == "spam"
