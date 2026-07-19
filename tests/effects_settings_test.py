"""Tests for lib_shared.models.EffectsSettings (v2 config block).

Covers:
- Default values (7 enabled + 2 disabled canonical effects) read via
  the loader
- Round-trip via from_dict / to_dict
- Validation (out-of-range pacing, bad lookback_days, unknown
  selector_algorithm, malformed entries)
- Wire shape (what the device + admin UI consume)

`_DEFAULT_EFFECTS_LIST_FULL` is gone — the canonical list now lives in
`lib_shared/config/effects_settings.json` and is read via
`lib_shared.effects_loader.load_effects_settings()`. These tests cover
the dataclass; the JSON / loader side is covered in
`tests/effects_loader_test.py`.
"""

import pytest

import lib_shared.effects_loader as effects_loader
from lib_shared.models import EffectsSettings


@pytest.fixture(autouse=True)
def _reset_loader_cache():
    """Clear the loader cache before each test so `_default_effects_list`
    inside `models.py` re-reads from the loader."""
    effects_loader.reset_effects_settings()
    # Also clear the per-function cache so a fresh list is built.
    from lib_shared.models import _default_effects_list

    if hasattr(_default_effects_list, "_cache"):
        delattr(_default_effects_list, "_cache")
    yield
    effects_loader.reset_effects_settings()
    if hasattr(_default_effects_list, "_cache"):
        delattr(_default_effects_list, "_cache")


def _canonical_entries():
    """Return the canonical effects list (loaded via the loader)."""
    cfg = effects_loader.load_effects_settings()
    return [{"name": e["name"], "enabled": e["enabled"]} for e in cfg["effects"]]


def test_default_instantiation_uses_canonical_list():
    """A no-arg constructor picks up the canonical 9-entry list
    (PngDisplay/VideoDisplay removed in #38 — those are now inner
    renderers consumed by MediaCycler, not registry entries; Flame
    removed as well)."""
    s = EffectsSettings()
    assert len(s.effects) == 9
    # All nine are enabled by default.
    enabled = [e["name"] for e in s.effects if e["enabled"]]
    assert len(enabled) == 9
    assert "Hyperspace" in enabled
    assert "WindFire" in enabled
    assert "CoronalMassEjection" in enabled
    assert "Eyeball" in enabled
    assert "Marble" in enabled
    assert "Metaballs" in enabled
    assert "Fireworks" in enabled
    assert "NightSky" in enabled
    assert "Honeycomb" in enabled
    # Inner renderers consumed by MediaCycler (not registry entries)
    assert "VideoDisplay" not in enabled
    assert "PngDisplay" not in enabled
    assert "ImageDisplay" not in enabled


def test_default_pacing_values():
    """The historic pacing values are preserved as defaults."""
    s = EffectsSettings()
    assert s.fade_seconds == 2.0
    assert s.hold_seconds == 15.0
    assert s.intro_seconds == 5.0
    assert s.idle_seconds == 300.0
    assert s.lookback_days == 14
    assert s.selector_algorithm == "weighted"


def test_canonical_list_has_known_names():
    """The canonical list contains exactly the 9 expected effect names
    (PngDisplay/VideoDisplay removed in #38; Flame removed)."""
    names = {e["name"] for e in _canonical_entries()}
    assert names == {
        "Hyperspace",
        "Honeycomb",
        "WindFire",
        "CoronalMassEjection",
        "Eyeball",
        "Marble",
        "Metaballs",
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
    assert "recent_count" not in d  # dropped in #26 redesign (replaced by lookback_days + selector_algorithm)
    assert "lookback_days" in d
    assert "selector_algorithm" in d


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
    assert s2.lookback_days == s.lookback_days
    assert s2.selector_algorithm == s.selector_algorithm


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
            {"name": "Flame", "enabled": False},
        ],
        "fade_seconds": 1.5,
        "hold_seconds": 7.0,
        "intro_seconds": 3.0,
        "idle_seconds": 60.0,
        "lookback_days": 21,
        "selector_algorithm": "weighted",
    }
    s = EffectsSettings.from_dict(d)
    assert s.effects == d["effects"]
    assert s.fade_seconds == 1.5
    assert s.hold_seconds == 7.0
    assert s.intro_seconds == 3.0
    assert s.idle_seconds == 60.0
    assert s.lookback_days == 21
    assert s.selector_algorithm == "weighted"


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


def test_validate_lookback_days_below_min_raises():
    """validate() raises ValueError on `lookback_days` below MIN_LOOKBACK_DAYS."""
    from lib_shared.models import EffectsSettings as _ES

    s = EffectsSettings(lookback_days=_ES.MIN_LOOKBACK_DAYS - 1)
    with pytest.raises(ValueError):
        s.validate()


def test_validate_lookback_days_above_max_raises():
    """validate() raises ValueError on `lookback_days` above MAX_LOOKBACK_DAYS."""
    from lib_shared.models import EffectsSettings as _ES

    s = EffectsSettings(lookback_days=_ES.MAX_LOOKBACK_DAYS + 1)
    with pytest.raises(ValueError):
        s.validate()


def test_validate_selector_algorithm_unknown_raises():
    """validate() raises ValueError on unknown `selector_algorithm`."""
    s = EffectsSettings(selector_algorithm="not-a-real-algorithm")
    with pytest.raises(ValueError):
        s.validate()


def test_validate_lookback_days_non_int_raises():
    """validate() raises ValueError on non-int `lookback_days` (e.g. a float)."""
    s = EffectsSettings(lookback_days=14.5)  # type: ignore[arg-type]
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
    src: list = [{"name": "Flame", "enabled": True}]
    s = EffectsSettings(effects=src)
    src.append({"name": "Hyperspace", "enabled": True})
    # Mutating the source list doesn't affect the constructed instance.
    assert len(s.effects) == 1


def test_default_pacing_reads_from_override(tmp_path, monkeypatch):
    """Pacing fields default to the loader's value (canonical or override).

    Without an override, the loader returns the canonical pacing — the
    `test_default_pacing_values` assertions above pin that. With an
    override file set via `EFFECTS_SETTINGS_OVERRIDE`, the same no-arg
    constructor picks up the override's pacing instead, so an operator
    who edits only `idle_seconds` (etc.) sees the change on the device
    even when the device boots with no wire envelope sync.

    This pins the `idle_seconds=30` override scenario the device was
    failing on 2026-07-09: the override file's pacing fields were
    silently discarded because the constructor hardcoded `300.0`.
    """
    import json

    override = tmp_path / "effects_settings.json"
    override.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "effects": [
                    {
                        "name": "Fireworks",
                        "enabled": True,
                        "module": "lib_shared.patterns.fireworks",
                        "class_name": "Fireworks",
                    }
                ],
                "fade_seconds": 1.0,
                "hold_seconds": 7.0,
                "intro_seconds": 3.0,
                "idle_seconds": 30.0,
                "lookback_days": 21,
                "selector_algorithm": "weighted",
            }
        )
    )
    monkeypatch.setenv("EFFECTS_SETTINGS_OVERRIDE", str(override))
    effects_loader.reset_effects_settings()

    s = EffectsSettings()
    assert s.fade_seconds == 1.0
    assert s.hold_seconds == 7.0
    assert s.intro_seconds == 3.0
    assert s.idle_seconds == 30.0
    assert s.lookback_days == 21
    assert s.selector_algorithm == "weighted"


def test_from_dict_empty_dict_reads_pacing_from_override(tmp_path, monkeypatch):
    """`from_dict({})` on a device with no wire envelope still honors
    the operator's override pacing — the override file is the
    sole source of truth when the wire is silent."""
    import json

    override = tmp_path / "effects_settings.json"
    override.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "effects": [
                    {
                        "name": "Hyperspace",
                        "enabled": True,
                        "module": "lib_shared.patterns.hyperspace",
                        "class_name": "Hyperspace",
                    }
                ],
                "fade_seconds": 1.0,
                "hold_seconds": 7.0,
                "intro_seconds": 3.0,
                "idle_seconds": 30.0,
                "lookback_days": 21,
                "selector_algorithm": "weighted",
            }
        )
    )
    monkeypatch.setenv("EFFECTS_SETTINGS_OVERRIDE", str(override))
    effects_loader.reset_effects_settings()

    # Empty wire envelope → every pacing field is missing → must fall
    # through to the loader, which honors the override.
    s = EffectsSettings.from_dict({})
    assert s.idle_seconds == 30.0
    assert s.fade_seconds == 1.0
    assert s.hold_seconds == 7.0
    assert s.intro_seconds == 3.0
    assert s.lookback_days == 21
    assert s.selector_algorithm == "weighted"
