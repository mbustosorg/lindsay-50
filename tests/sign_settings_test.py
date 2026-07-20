"""Tests for lib_shared.models.SignSettings (v3 nested block).

Covers constructor defaults, from_dict / to_dict / round-trip,
and the TypeError guarantees on the removed top-level kwargs.
"""

import pytest

from lib_shared.models import SignConfig, SignSettings

# --- SignSettings standalone ---


def test_constructor_defaults():
    """SignSettings() carries the canonical sign_name + timezone + enforce_allowed_senders."""
    s = SignSettings()
    assert s.sign_name == "Lindsay's Heart"
    assert s.timezone == "US/Pacific"
    assert s.enforce_allowed_senders is True


def test_constructor_custom_values():
    """SignSettings(sign_name=..., timezone=..., enforce_allowed_senders=...) honors all three kwargs."""
    s = SignSettings(sign_name="Custom", timezone="UTC", enforce_allowed_senders=False)
    assert s.sign_name == "Custom"
    assert s.timezone == "UTC"
    assert s.enforce_allowed_senders is False


def test_from_dict_with_values():
    """SignSettings.from_dict reads all three keys."""
    s = SignSettings.from_dict(
        {"sign_name": "Alice's Sign", "timezone": "Europe/Paris", "enforce_allowed_senders": False}
    )
    assert s.sign_name == "Alice's Sign"
    assert s.timezone == "Europe/Paris"
    assert s.enforce_allowed_senders is False


def test_from_dict_empty_uses_defaults():
    """from_dict({}) yields defaults — back-compat for partial payloads."""
    s = SignSettings.from_dict({})
    assert s.sign_name == "Lindsay's Heart"
    assert s.timezone == "US/Pacific"
    assert s.enforce_allowed_senders is True


def test_from_dict_none_uses_defaults():
    """from_dict(None) yields defaults — back-compat for absent payloads."""
    s = SignSettings.from_dict(None)
    assert s.sign_name == "Lindsay's Heart"
    assert s.timezone == "US/Pacific"
    assert s.enforce_allowed_senders is True


def test_to_dict_contains_all_three_keys():
    """to_dict always emits all three keys (no conditional omission)."""
    s = SignSettings()
    assert s.to_dict() == {
        "sign_name": "Lindsay's Heart",
        "timezone": "US/Pacific",
        "enforce_allowed_senders": True,
    }


def test_round_trip_lossless():
    """from_dict(to_dict(s)) == s."""
    s = SignSettings(sign_name="X", timezone="America/Chicago", enforce_allowed_senders=False)
    s2 = SignSettings.from_dict(s.to_dict())
    assert s2.sign_name == s.sign_name
    assert s2.timezone == s.timezone
    assert s2.enforce_allowed_senders == s.enforce_allowed_senders


# --- SignConfig wiring with the new sign_settings kwarg ---


def test_sign_config_accepts_sign_settings_kwarg():
    """SignConfig(sign_settings=SignSettings(...)) works."""
    s = SignSettings(sign_name="Wire", timezone="UTC")
    c = SignConfig(sign_settings=s)
    assert c.sign_settings is s


def test_sign_config_accepts_dict_sign_settings():
    """SignConfig(sign_settings={...}) also works (parses via from_dict)."""
    c = SignConfig(sign_settings={"sign_name": "DictSign", "timezone": "Europe/London"})
    assert c.sign_settings.sign_name == "DictSign"
    assert c.sign_settings.timezone == "Europe/London"


def test_sign_config_rejects_legacy_sign_kwarg():
    """SignConfig(sign=...) raises TypeError — attribute was renamed."""
    with pytest.raises(TypeError):
        SignConfig(sign=SignSettings())  # type: ignore[call-arg]


def test_sign_config_rejects_legacy_timezone_kwarg():
    """SignConfig(timezone=...) raises TypeError — top-level parameter removed."""
    with pytest.raises(TypeError):
        SignConfig(timezone="UTC")  # type: ignore[call-arg]


def test_sign_config_rejects_legacy_enforcement_enabled_kwarg():
    """SignConfig(enforcement_enabled=...) raises TypeError — top-level parameter removed."""
    with pytest.raises(TypeError):
        SignConfig(enforcement_enabled=False)  # type: ignore[call-arg]


def test_sign_config_rejects_legacy_name_display_format_kwarg():
    """SignConfig(name_display_format=...) raises TypeError — top-level parameter removed."""
    with pytest.raises(TypeError):
        SignConfig(name_display_format="full")  # type: ignore[call-arg]


def test_to_dict_sign_settings_block_emitted():
    """to_dict on SignConfig emits sign_settings with all three keys, no top-level sign/timezone."""
    c = SignConfig(sign_settings=SignSettings(sign_name="Wire", timezone="US/Eastern"))
    d = c.to_dict()
    assert d["sign_settings"]["sign_name"] == "Wire"
    assert d["sign_settings"]["timezone"] == "US/Eastern"
    assert d["sign_settings"]["enforce_allowed_senders"] is True
    assert "sign" not in d
    assert "timezone" not in d
    assert "enforcement_enabled" not in d
    assert "enforce_allowed_senders" not in d
    assert "name_display_format" not in d


def test_to_dict_includes_text_and_effects_blocks():
    """to_dict also includes the v3 text_settings (with name_display_format)
    and effects_settings (no v3-specific extras)."""
    c = SignConfig()
    d = c.to_dict()
    assert "name_display_format" in d["text_settings"]
    assert d["text_settings"]["name_display_format"] == "first_initial_if_duplicates"
    # effects_settings block has no v3-specific extras; the basic pacing
    # fields are still there.
    assert "fade_seconds" in d["effects_settings"]
    assert "name_display_format" not in d["effects_settings"]
    assert "enforcement_enabled" not in d["effects_settings"]
