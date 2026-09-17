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


def test_to_dict_contains_all_five_keys():
    """to_dict always emits all five keys (no conditional omission).

    v2 (issue #51): adds `target_version` as a fourth key. v3 (issue #71
    follow-up, round 11): adds `pinned_version` as a fifth key. The
    default for `target_version` resolves to Flask's running short SHA
    at construction time — the expected value is whatever
    `short_sha(git rev-parse HEAD)` returns, so the test asserts the
    five-field shape and that `target_version` is a non-empty string.
    `pinned_version` defaults to "" (operator hasn't pinned anything).
    """
    s = SignSettings()
    out = s.to_dict()
    assert set(out.keys()) == {
        "sign_name",
        "timezone",
        "enforce_allowed_senders",
        "pinned_version",
        "target_version",
    }
    assert out["sign_name"] == "Lindsay's Heart"
    assert out["timezone"] == "US/Pacific"
    assert out["enforce_allowed_senders"] is True
    assert out["pinned_version"] == ""
    assert isinstance(out["target_version"], str) and out["target_version"]


def test_round_trip_lossless():
    """from_dict(to_dict(s)) == s."""
    s = SignSettings(sign_name="X", timezone="America/Chicago", enforce_allowed_senders=False)
    s2 = SignSettings.from_dict(s.to_dict())
    assert s2.sign_name == s.sign_name
    assert s2.timezone == s.timezone
    assert s2.enforce_allowed_senders == s.enforce_allowed_senders


# --- Round 11: pinned_version split (issue #71 follow-up) ---


def test_constructor_accepts_pinned_version():
    """SignSettings(pinned_version=...) stores the operator's raw input verbatim."""
    s = SignSettings(pinned_version="abc1234")
    assert s.pinned_version == "abc1234"


def test_constructor_pinned_version_default_is_empty():
    """SignSettings() defaults pinned_version to '' (empty == inherit Flask)."""
    s = SignSettings()
    assert s.pinned_version == ""


def test_from_dict_reads_pinned_version():
    """from_dict({'pinned_version': 'def5678'}) preserves the operator's raw input."""
    s = SignSettings.from_dict({"pinned_version": "def5678"})
    assert s.pinned_version == "def5678"


def test_from_dict_missing_pinned_version_defaults_to_empty():
    """from_dict({}) yields pinned_version='' (back-compat for pre-r11 wire forms)."""
    s = SignSettings.from_dict({})
    assert s.pinned_version == ""


def test_from_dict_pinned_version_none_becomes_empty():
    """from_dict({'pinned_version': None}) normalizes to '' (None means 'inherit')."""
    s = SignSettings.from_dict({"pinned_version": None})
    assert s.pinned_version == ""


def test_to_dict_emits_both_pinned_and_target():
    """to_dict() emits BOTH pinned_version and target_version (round-11 wire shape).

    The wire form must be symmetric with the disk form so the
    /api/config response and the SQLite row carry the same fields.
    The Pi's /api/sign/settings consumer reads target_version; the
    /settings UI reads pinned_version. Both must always be present
    on the wire.

    Note: `_resolve_pinned_to_target` is a Flask-side helper that
    runs in `_save_and_publish`, NOT in the constructor — the
    constructor only auto-resolves the legacy `target_version`
    field. So `SignSettings(pinned_version="abc1234")` leaves
    `target_version` at its construction-time default (Flask's
    running short SHA). This is the expected round-11 invariant:
    the constructor is wire-shape-only; resolution happens on save.
    """
    s = SignSettings(pinned_version="abc1234")
    out = s.to_dict()
    assert "pinned_version" in out
    assert "target_version" in out
    assert out["pinned_version"] == "abc1234"
    # target_version carries the construction-time default
    # (Flask's running short SHA). The save flow resolves via
    # pinned_version afterward, but that lives in main.py.
    assert out["target_version"] != ""
    assert isinstance(out["target_version"], str)


def test_to_dict_emits_empty_pinned_when_unset():
    """to_dict() emits pinned_version="" when the operator cleared the pin."""
    s = SignSettings()  # no pinned_version
    out = s.to_dict()
    assert "pinned_version" in out
    assert out["pinned_version"] == ""
    # target_version still resolves to Flask's running short SHA
    assert isinstance(out["target_version"], str) and out["target_version"]


def test_round_trip_preserves_pinned_version():
    """from_dict(to_dict(s)) preserves pinned_version through the round-trip."""
    s = SignSettings(pinned_version="abc1234")
    s2 = SignSettings.from_dict(s.to_dict())
    assert s2.pinned_version == s.pinned_version
    # target_version also round-trips (both fields make the trip)
    assert s2.target_version == s.target_version


def test_to_dict_includes_five_keys_after_round_11():
    """to_dict() emits exactly five keys after the round-11 pinned_version addition.

    Pin the wire shape so future field additions are intentional,
    not silent drift.
    """
    s = SignSettings(pinned_version="abc1234")
    out = s.to_dict()
    assert set(out.keys()) == {
        "sign_name",
        "timezone",
        "enforce_allowed_senders",
        "pinned_version",
        "target_version",
    }


def test_from_dict_legacy_wire_form_with_only_target_version():
    """from_dict({'target_version': 'abc1234'}) — pre-r11 wire shape — works.

    A pre-r11 Flask publishes payloads with only `target_version`
    and no `pinned_version`. The loader must still accept this
    shape: it round-trips into `pinned_version=""` and
    `target_version="abc1234"`.
    """
    s = SignSettings.from_dict({"target_version": "abc1234"})
    assert s.pinned_version == ""
    assert s.target_version == "abc1234"


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
