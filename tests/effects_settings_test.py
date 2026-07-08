"""Tests for lib_shared.models.EffectsSettings (v2 config block).

Covers:
- Default values (7 enabled + 2 disabled canonical effects)
- Round-trip via from_dict / to_dict
- Validation (out-of-range pacing, bad recent_count, malformed entries)
- Wire shape (what the device + admin UI consume)
"""

import pytest

from lib_shared.models import EffectsSettings, _DEFAULT_EFFECTS_LIST_FULL


def test_default_instantiation_uses_canonical_list():
    """A no-arg constructor picks up the 9-entry canonical list."""
    s = EffectsSettings()
    assert len(s.effects) == 9
    # Seven enabled by default; the two asset-dependent patterns are disabled.
    enabled = [e["name"] for e in s.effects if e["enabled"]]
    assert len(enabled) == 7
    assert "Hyperspace" in enabled
    assert "Fireworks" in enabled
    assert "NightSky" in enabled
    assert "Honeycomb" in enabled
    assert "WindFire" in enabled
    assert "CoronalMassEjection" in enabled
    assert "Eyeball" in enabled
    # Disabled-by-default
    assert "VideoDisplay" not in enabled
    assert "PngDisplay" not in enabled


def test_default_pacing_values():
    """The historic pacing values are preserved as defaults."""
    s = EffectsSettings()
    assert s.fade_seconds == 2.0
    assert s.hold_seconds == 15.0
    assert s.intro_seconds == 5.0
    assert s.idle_seconds == 300.0
    assert s.recent_count == 5


def test_canonical_list_has_known_names():
    """The canonical list contains exactly the 9 expected effect names."""
    names = {e["name"] for e in _DEFAULT_EFFECTS_LIST_FULL}
    assert names == {
        "Hyperspace",
        "VideoDisplay",
        "PngDisplay",
        "Honeycomb",
        "WindFire",
        "CoronalMassEjection",
        "Eyeball",
        "Fireworks",
        "NightSky",
    }


def test_to_dict_contains_all_fields():
    """to_dict emits the full wire shape."""
    s = EffectsSettings()
    d = s.to_dict()
    assert "effects" in d
    assert "fade_seconds" in d
    assert "hold_seconds" in d
    assert "intro_seconds" in d
    assert "idle_seconds" in d
    assert "recent_count" in d


def test_round_trip_default():
    """from_dict(to_dict(s)) == s (defaults)."""
    s = EffectsSettings()
    d = s.to_dict()
    s2 = EffectsSettings.from_dict(d)
    assert s2.effects == s.effects
    assert s2.fade_seconds == s.fade_seconds
    assert s2.hold_seconds == s.hold_seconds
    assert s2.intro_seconds == s.intro_seconds
    assert s2.idle_seconds == s.idle_seconds
    assert s2.recent_count == s.recent_count


def test_from_dict_accepts_empty_dict():
    """An empty dict is valid and yields the canonical defaults."""
    s = EffectsSettings.from_dict({})
    assert len(s.effects) == 9
    assert s.fade_seconds == 2.0


def test_from_dict_none_uses_defaults():
    """from_dict(None) yields the canonical defaults."""
    s = EffectsSettings.from_dict(None)
    assert len(s.effects) == 9


def test_from_dict_with_custom_values():
    """from_dict picks up custom pacing and effect selections."""
    d = {
        "effects": [
            {"name": "Hyperspace", "enabled": True},
            {"name": "Fireworks", "enabled": False},
        ],
        "fade_seconds": 1.5,
        "hold_seconds": 7.0,
        "intro_seconds": 3.0,
        "idle_seconds": 60.0,
        "recent_count": 10,
    }
    s = EffectsSettings.from_dict(d)
    assert s.effects == d["effects"]
    assert s.fade_seconds == 1.5
    assert s.hold_seconds == 7.0
    assert s.intro_seconds == 3.0
    assert s.idle_seconds == 60.0
    assert s.recent_count == 10


def test_from_dict_rejects_malformed_effects_list():
    """from_dict raises ValueError when `effects` isn't a list of dicts."""
    with pytest.raises(ValueError):
        EffectsSettings.from_dict({"effects": "not a list"})
    with pytest.raises(ValueError):
        EffectsSettings.from_dict({"effects": [42]})
    with pytest.raises(ValueError):
        EffectsSettings.from_dict({"effects": [{"name": "Foo"}]})  # missing enabled
    with pytest.raises(ValueError):
        EffectsSettings.from_dict({"effects": [{"enabled": True}]})  # missing name


def test_from_dict_rejects_non_string_name():
    """from_dict raises ValueError when a name is not a string."""
    with pytest.raises(ValueError):
        EffectsSettings.from_dict({"effects": [{"name": 123, "enabled": True}]})


def test_validate_negative_pacing_raises():
    """validate() raises ValueError on a negative pacing value."""
    s = EffectsSettings(fade_seconds=-1.0)
    with pytest.raises(ValueError):
        s.validate()


def test_validate_recent_count_zero_raises():
    """validate() raises ValueError on recent_count < 1."""
    s = EffectsSettings(recent_count=0)
    with pytest.raises(ValueError):
        s.validate()


def test_validate_recent_count_negative_raises():
    """validate() raises ValueError on negative recent_count."""
    s = EffectsSettings(recent_count=-3)
    with pytest.raises(ValueError):
        s.validate()


def test_validate_malformed_effects_raises():
    """validate() raises ValueError when an effects entry is not a dict."""
    s = EffectsSettings(effects=["Hyperspace"])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        s.validate()


def test_validate_accepts_zero_pacing():
    """Zero pacing values are valid (no fade, instant transitions)."""
    s = EffectsSettings(fade_seconds=0.0, hold_seconds=0.0, intro_seconds=0.0, idle_seconds=0.0)
    s.validate()  # no raise


def test_constructor_copies_effects_list():
    """The constructor copies the effects list (not the same reference)."""
    src: list = [{"name": "Fireworks", "enabled": True}]
    s = EffectsSettings(effects=src)
    src.append({"name": "Hyperspace", "enabled": True})
    # Mutating the source list doesn't affect the constructed instance.
    assert len(s.effects) == 1
