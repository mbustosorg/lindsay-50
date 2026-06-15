"""Tests for lib_shared.models.TextSettings (v2 config block).

Covers:
- Default values (frame_delay, offset_seconds, color, text_effect)
- TEXT_EFFECTS whitelist
- Round-trip via from_dict / to_dict
- Validation (out-of-range pacing, bad color, unknown text_effect)
- Wire shape
"""

import pytest

from lib_shared.models import TextSettings


def test_default_values():
    """The historic per-field defaults are preserved."""
    s = TextSettings()
    assert s.frame_delay == 0.04
    assert s.offset_seconds == 1.0
    assert s.color == 0xFF0000
    assert s.text_effect == "scroll"


def test_text_effects_whitelist():
    """TEXT_EFFECTS contains exactly one entry: 'scroll'."""
    assert TextSettings.TEXT_EFFECTS == ("scroll",)


def test_to_dict_contains_all_fields():
    """to_dict emits the four-field wire shape."""
    d = TextSettings().to_dict()
    assert set(d.keys()) == {"frame_delay", "offset_seconds", "color", "text_effect"}


def test_round_trip_default():
    """from_dict(to_dict(s)) == s (defaults)."""
    s = TextSettings()
    s2 = TextSettings.from_dict(s.to_dict())
    assert s2.frame_delay == s.frame_delay
    assert s2.offset_seconds == s.offset_seconds
    assert s2.color == s.color
    assert s2.text_effect == s.text_effect


def test_from_dict_accepts_empty_dict():
    """An empty dict yields the canonical defaults."""
    s = TextSettings.from_dict({})
    assert s.frame_delay == 0.04
    assert s.color == 0xFF0000


def test_from_dict_none_uses_defaults():
    """from_dict(None) yields the canonical defaults."""
    s = TextSettings.from_dict(None)
    assert s.color == 0xFF0000


def test_from_dict_with_custom_values():
    """from_dict picks up custom field values."""
    s = TextSettings.from_dict(
        {
            "frame_delay": 0.02,
            "offset_seconds": 2.5,
            "color": 0x00FF00,
            "text_effect": "scroll",
        }
    )
    assert s.frame_delay == 0.02
    assert s.offset_seconds == 2.5
    assert s.color == 0x00FF00
    assert s.text_effect == "scroll"


def test_from_dict_rejects_unknown_text_effect():
    """from_dict raises ValueError on an unknown text_effect value."""
    with pytest.raises(ValueError):
        TextSettings.from_dict({"text_effect": "spiral"})


def test_validate_negative_frame_delay_raises():
    """validate() raises on negative frame_delay."""
    s = TextSettings(frame_delay=-0.01)
    with pytest.raises(ValueError):
        s.validate()


def test_validate_negative_offset_raises():
    """validate() raises on negative offset_seconds."""
    s = TextSettings(offset_seconds=-1.0)
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


def test_validate_zero_pacing_accepted():
    """Zero frame_delay / offset_seconds are valid."""
    s = TextSettings(frame_delay=0.0, offset_seconds=0.0)
    s.validate()  # no raise


def test_validate_unknown_text_effect_raises():
    """validate() raises on an unknown text_effect value."""
    s = TextSettings(text_effect="bounce")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        s.validate()
