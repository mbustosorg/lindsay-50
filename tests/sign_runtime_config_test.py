"""Tests for lib_shared.models.SignConfig — the v3 wire shape + migration.

Covers:
- CURRENT_VERSION is 3
- Default SignConfig carries the v3 blocks
- from_dict runs the migration registry
- to_dict emits the v3 shape (sign_settings with sign_name + timezone,
  no top-level sign/timezone/enforcement_enabled/name_display_format)
- update_from_dict only overwrites the new blocks when present
- update_from_dict migrates v1 and v2 inputs transparently
"""

from lib_shared.config_migrations import migrate
from lib_shared.models import (
    EffectsSettings,
    FilterRule,
    SignConfig,
    SignSettings,
    TextSettings,
)


def test_current_version_is_3():
    """SignConfig.CURRENT_VERSION is 3 (matches the registered migration)."""
    assert SignConfig.CURRENT_VERSION == 3


def test_default_sign_config_has_v3_blocks():
    """A no-arg SignConfig carries EffectsSettings + TextSettings + SignSettings (v3 blocks)."""
    c = SignConfig()
    assert isinstance(c.effects_settings, EffectsSettings)
    assert isinstance(c.text_settings, TextSettings)
    assert isinstance(c.sign_settings, SignSettings)
    assert c.version == 3


def test_default_sign_config_has_no_legacy_top_level_fields():
    """The legacy top-level sign/timezone/allowed_senders fields are gone."""
    c = SignConfig()
    assert not hasattr(c, "sign") or isinstance(getattr(c, "sign", None), type(None))
    assert not hasattr(c, "timezone")
    assert not hasattr(c, "allowed_senders")
    assert not hasattr(c, "enforcement_enabled")
    assert not hasattr(c, "name_display_format")


def test_to_dict_omits_legacy_fields():
    """to_dict does not emit top-level sign/timezone/enforcement_enabled/name_display_format."""
    c = SignConfig()
    d = c.to_dict()
    assert "sign" not in d
    assert "timezone" not in d
    assert "enforcement_enabled" not in d
    assert "name_display_format" not in d
    assert "allowed_senders" not in d
    assert "sign_settings" in d
    assert "effects_settings" in d
    assert "text_settings" in d
    assert "senders" in d


def test_default_text_settings_speed_is_3():
    """Default TextSettings carries speed=3 (Medium)."""
    c = SignConfig()
    assert c.text_settings.speed == 3


def test_default_text_settings_enforcement_enabled_true():
    """Default TextSettings has enforcement_enabled=True (senders master toggle on)."""
    c = SignConfig()
    assert c.text_settings.enforcement_enabled is True


def test_default_sign_settings():
    """Default SignSettings carries the canonical sign_name + timezone."""
    c = SignConfig()
    assert c.sign_settings.sign_name == "Lindsay's Heart"
    assert c.sign_settings.timezone == "US/Pacific"


def test_from_dict_default():
    """from_dict({}) yields a default v3 SignConfig."""
    c = SignConfig.from_dict({})
    assert c.version == 3
    assert isinstance(c.effects_settings, EffectsSettings)
    assert isinstance(c.text_settings, TextSettings)
    assert isinstance(c.sign_settings, SignSettings)


def test_from_dict_runs_v1_migration():
    """A v1 payload (with tz_offset_mins + rendering) is migrated to v3."""
    v1 = {
        "filters": [],
        "senders": [],
        "sign": {"name": "Old Sign"},
        "timezone": "America/Los_Angeles",
        "version": 1,
        "tz_offset_mins": -420,
        "rendering": {"mode": "scroll", "speed": 0.5, "color": 0xFFFFFF},
    }
    c = SignConfig.from_dict(v1)
    assert c.version == 3
    assert isinstance(c.effects_settings, EffectsSettings)
    assert isinstance(c.text_settings, TextSettings)
    # `sign.name` migrated into `sign_settings.sign_name`.
    assert c.sign_settings.sign_name == "Old Sign"
    # `timezone` migrated into `sign_settings.timezone`.
    assert c.sign_settings.timezone == "America/Los_Angeles"


def test_from_dict_runs_v2_migration():
    """A v2 payload (already with effects_settings/text_settings) migrates to v3."""
    v2 = {
        "version": 2,
        "filters": [],
        "senders": [],
        "effects_settings": EffectsSettings().to_dict(),
        "text_settings": TextSettings().to_dict(),
        "sign": {"name": "Mid Sign"},
        "timezone": "America/Chicago",
    }
    c = SignConfig.from_dict(v2)
    assert c.version == 3
    assert c.sign_settings.sign_name == "Mid Sign"
    assert c.sign_settings.timezone == "America/Chicago"


def test_from_dict_preserves_filters_and_senders():
    """v3 migration preserves the filter and sender lists."""
    v1 = {
        "filters": [{"type": "keyword", "pattern": "spam", "action": "suppress"}],
        "senders": [{"phone": "+15551112222", "name": "Mom"}],
        "sign": {"name": "X"},
        "timezone": "US/Pacific",
        "version": 1,
        "tz_offset_mins": -420,
    }
    c = SignConfig.from_dict(v1)
    assert len(c.filters) == 1
    assert c.filters[0].type == "keyword"
    assert c.filters[0].pattern == "spam"
    # senders now keyed by normalized phone → dict-of-fields
    assert c.senders["+15551112222"]["name"] == "Mom"


def test_to_dict_from_dict_round_trip():
    """to_dict / from_dict round-trips cleanly."""
    c = SignConfig(
        filters=[FilterRule(type="keyword", pattern="spam", action="suppress", status="enabled")],
        senders={"+15550001111": {"name": "Alice", "allowed": True, "phone": "+15550001111"}},
        sign_settings=SignSettings(sign_name="Round Trip", timezone="America/Chicago"),
        effects_settings=EffectsSettings(fade_seconds=1.0, hold_seconds=10.0),
        text_settings=TextSettings(color=0x00FF00),
    )
    d = c.to_dict()
    c2 = SignConfig.from_dict(d)
    assert c2.sign_settings.sign_name == "Round Trip"
    assert c2.sign_settings.timezone == "America/Chicago"
    assert c2.effects_settings.fade_seconds == 1.0
    assert c2.effects_settings.hold_seconds == 10.0
    assert c2.text_settings.color == 0x00FF00
    assert c2.senders["+15550001111"]["name"] == "Alice"
    assert len(c2.filters) == 1
    assert c2.filters[0].pattern == "spam"


def test_update_from_dict_replaces_fields():
    """update_from_dict replaces all fields from the given dict."""
    c = SignConfig()
    c.update_from_dict(
        {
            "filters": [{"type": "regex", "pattern": "x", "action": "suppress", "status": "enabled"}],
            "senders": [{"phone": "+1", "name": "n", "allowed": True}],
            "sign_settings": {"sign_name": "New", "timezone": "UTC"},
            "version": 3,
            "effects_settings": {"fade_seconds": 0.5, "hold_seconds": 5.0},
            "text_settings": {"color": 0x0000FF},
        }
    )
    assert c.sign_settings.sign_name == "New"
    assert c.sign_settings.timezone == "UTC"
    assert c.effects_settings.fade_seconds == 0.5
    assert c.text_settings.color == 0x0000FF


def test_update_from_dict_migrates_v1_input():
    """update_from_dict transparently migrates a v1 dict to v3."""
    c = SignConfig()
    c.update_from_dict(
        {
            "filters": [],
            "senders": [],
            "sign": {"name": "Migrated"},
            "timezone": "US/Pacific",
            "version": 1,
            "tz_offset_mins": -420,
            "rendering": {"mode": "scroll", "speed": 0.5, "color": 0},
        }
    )
    assert c.version == 3
    assert c.sign_settings.sign_name == "Migrated"
    assert isinstance(c.effects_settings, EffectsSettings)
    assert isinstance(c.text_settings, TextSettings)


def test_update_method_copies_fields():
    """SignConfig.update(other) replaces all fields from the other instance."""
    a = SignConfig()
    b = SignConfig(sign_settings=SignSettings(sign_name="FromB", timezone="UTC"))
    a.update(b)
    assert a.sign_settings.sign_name == "FromB"
    assert a.sign_settings.timezone == "UTC"


def test_sign_config_classmethod_default():
    """SignConfig.default() returns a fresh default."""
    c = SignConfig.default()
    assert c.version == 3
    assert isinstance(c.effects_settings, EffectsSettings)


def test_from_dict_with_constructors():
    """SignConfig(...) can take EffectsSettings/TextSettings instances directly."""
    es = EffectsSettings(fade_seconds=1.0)
    ts = TextSettings(color=0xABCDEF)
    c = SignConfig(effects_settings=es, text_settings=ts)
    assert c.effects_settings is es
    assert c.text_settings is ts


def test_migrate_helper_brings_v1_to_v3():
    """The standalone migrate() helper handles v1 → v3."""
    v1 = {
        "filters": [],
        "senders": [],
        "version": 1,
        "tz_offset_mins": -420,
        "rendering": {"mode": "scroll", "speed": 0.5},
    }
    out = migrate(v1, current_version=SignConfig.CURRENT_VERSION)
    assert out["version"] == 3
    assert "tz_offset_mins" not in out
    assert "rendering" not in out
    assert "sign_settings" in out
    assert "effects_settings" in out
    assert "text_settings" in out


def test_migrate_helper_no_op_for_v3():
    """migrate() on a v3 input is a no-op (returns input unchanged)."""
    v3 = SignConfig().to_dict()
    out = migrate(v3, current_version=SignConfig.CURRENT_VERSION)
    assert out == v3


def test_update_from_dict_preserves_existing_blocks_when_absent():
    """When a payload omits effects_settings, the in-memory block is preserved."""
    c = SignConfig(
        effects_settings=EffectsSettings(fade_seconds=7.0),
        text_settings=TextSettings(color=0x123456),
    )
    c.update_from_dict(
        {
            "filters": [],
            "senders": [],
            "sign_settings": {"sign_name": "Partial", "timezone": "UTC"},
            "version": 3,
        }
    )
    # Pre-existing block values are kept (the v3-only update didn't touch them).
    assert c.effects_settings.fade_seconds == 7.0
    assert c.text_settings.color == 0x123456
    assert c.sign_settings.sign_name == "Partial"
