"""Tests for lib_shared.models.TextSettings (v2 config block).

The wire shape is `speed` (1..5) + `color` + `text_effect`; the device
translates speed → frame_delay / offset_seconds inside the scroller
(see tests/text_speed_test.py for the mapping table).
"""

import pytest

from lib_shared.models import TextSettings


def test_default_values():
    """The historic defaults (color, text_effect) and the new speed=3 default."""
    s = TextSettings()
    assert s.speed == 3
    assert s.color == 0xFF0000
    assert s.text_effect == "scroll"


def test_text_effects_whitelist():
    """TEXT_EFFECTS contains exactly one entry: 'scroll'."""
    assert TextSettings.TEXT_EFFECTS == ("scroll",)


def test_to_dict_contains_wire_fields():
    """to_dict emits the wire fields including v3's enforcement_enabled."""
    d = TextSettings().to_dict()
    assert set(d.keys()) == {"speed", "color", "text_effect", "enforcement_enabled"}


def test_round_trip_default():
    """from_dict(to_dict(s)) == s (defaults)."""
    s = TextSettings()
    s2 = TextSettings.from_dict(s.to_dict())
    assert s2.speed == s.speed
    assert s2.color == s.color
    assert s2.text_effect == s.text_effect


def test_from_dict_accepts_empty_dict():
    """An empty dict yields the canonical defaults."""
    s = TextSettings.from_dict({})
    assert s.speed == 3
    assert s.color == 0xFF0000


def test_from_dict_none_uses_defaults():
    """from_dict(None) yields the canonical defaults."""
    s = TextSettings.from_dict(None)
    assert s.color == 0xFF0000
    assert s.speed == 3


def test_from_dict_with_custom_values():
    """from_dict picks up custom field values."""
    s = TextSettings.from_dict(
        {
            "speed": 5,
            "color": 0x00FF00,
            "text_effect": "scroll",
        }
    )
    assert s.speed == 5
    assert s.color == 0x00FF00
    assert s.text_effect == "scroll"


def test_from_dict_rejects_unknown_text_effect():
    """from_dict raises ValueError on an unknown text_effect value."""
    with pytest.raises(ValueError):
        TextSettings.from_dict({"text_effect": "spiral"})


def test_from_dict_rejects_speed_zero():
    """from_dict raises ValueError when speed < 1."""
    with pytest.raises(ValueError):
        TextSettings.from_dict({"speed": 0})


def test_from_dict_rejects_speed_six():
    """from_dict raises ValueError when speed > 5."""
    with pytest.raises(ValueError):
        TextSettings.from_dict({"speed": 6})


def test_from_dict_rejects_speed_non_int():
    """from_dict raises ValueError when speed is a non-int (str, float)."""
    with pytest.raises(ValueError):
        TextSettings.from_dict({"speed": "fast"})
    with pytest.raises(ValueError):
        TextSettings.from_dict({"speed": 3.0})


def test_from_dict_rejects_speed_bool():
    """from_dict raises ValueError on bool (Python's bool is an int subclass)."""
    with pytest.raises(ValueError):
        TextSettings.from_dict({"speed": True})


def test_from_dict_ignores_legacy_frame_delay():
    """Old v2 payloads with frame_delay / offset_seconds don't crash; the
    new `speed` defaults to 3 and the technical fields are silently dropped.
    """
    s = TextSettings.from_dict({"frame_delay": 0.02, "offset_seconds": 2.5, "color": 0x00FF00})
    assert s.speed == 3
    assert s.color == 0x00FF00


def test_validate_speed_zero_raises():
    """validate() raises when speed is out of range (0)."""
    s = TextSettings(speed=0)
    with pytest.raises(ValueError):
        s.validate()


def test_validate_speed_six_raises():
    """validate() raises when speed is out of range (6)."""
    s = TextSettings(speed=6)
    with pytest.raises(ValueError):
        s.validate()


def test_validate_speed_non_int_raises():
    """validate() raises on non-int speeds."""
    s = TextSettings(speed="fast")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        s.validate()


def test_validate_speed_bool_rejected():
    """validate() rejects bool (which is technically int in Python)."""
    s = TextSettings(speed=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        s.validate()


def test_validate_color_too_high_raises():
    """validate() raises when color > 0xFFFFFF."""
    s = TextSettings(color=0x01000000)
    with pytest.raises(ValueError):
        s.validate()


def test_validate_color_negative_raises():
    """validate() raises when color < 0."""
    s = TextSettings(color=-1)
    with pytest.raises(ValueError):
        s.validate()


def test_validate_color_zero_accepted():
    """validate() accepts color == 0 (treated as "off")."""
    s = TextSettings(color=0)
    s.validate()  # no raise


def test_validate_color_max_accepted():
    """validate() accepts color == 0xFFFFFF (white)."""
    s = TextSettings(color=0xFFFFFF)
    s.validate()  # no raise


def test_validate_unknown_text_effect_raises():
    """validate() raises on an unknown text_effect value."""
    s = TextSettings(text_effect="bounce")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        s.validate()
