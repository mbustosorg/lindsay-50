"""Tests for lib_shared SignConfig.senders shape + related v3 senders semantics.

Covers:
- v3 senders wire shape: list[{phone, name, allowed}] → in-memory dict-of-dict
  keyed by normalize_phone(phone)
- Defaults: empty dict, sign_settings, text_settings.enforcement_enabled,
  effects_settings.name_display_format
- from_dict / to_dict / round-trip with the new shape
- No `status` field on senders entries (lifecycle is the LIST-level
  text_settings.enforcement_enabled toggle)
- `allowed_senders` parameter is REMOVED
- should_render_sender scenarios end-to-end through MessageManager
- Unknown name_display_format is rejected
"""

import pytest

from lib_shared.messages import should_render_sender
from lib_shared.models import SignConfig

# --- SignConfig.senders shape (Section 3.4) ---


def test_default_senders_is_empty_dict_of_dict():
    """SignConfig().senders is an empty dict (no dict-of-str legacy shape)."""
    c = SignConfig()
    assert c.senders == {}


def test_default_sign_settings():
    """Default sign_settings carries the canonical sign_name + timezone."""
    c = SignConfig()
    assert c.sign_settings.sign_name == "Lindsay's Heart"
    assert c.sign_settings.timezone == "US/Pacific"


def test_default_enforcement_enabled_true():
    """Default text_settings.enforcement_enabled is True (senders master toggle on)."""
    c = SignConfig()
    assert c.text_settings.enforcement_enabled is True


def test_default_name_display_format():
    """Default effects_settings.name_display_format is "first_initial_if_duplicates"."""
    c = SignConfig()
    assert c.effects_settings.name_display_format == "first_initial_if_duplicates"


def test_from_dict_senders_list_shape():
    """from_dict reads the list-of-dicts shape with normalized phone keys."""
    c = SignConfig.from_dict({"senders": [{"phone": "+15551234567", "name": "Alice", "allowed": True}]})
    assert c.senders["+15551234567"] == {
        "name": "Alice",
        "allowed": True,
        "phone": "+15551234567",
    }


def test_from_dict_senders_back_compat_no_allowed_defaults_true():
    """A wire entry with no `allowed` field gets `allowed=True` (back-compat default)."""
    c = SignConfig.from_dict({"senders": [{"phone": "+15551234567", "name": "Alice"}]})
    assert c.senders["+15551234567"]["allowed"] is True


def test_from_dict_senders_no_status_field():
    """v3 senders entries have NO `status` field — there's no per-entry lifecycle."""
    c = SignConfig.from_dict({"senders": [{"phone": "+15551234567", "name": "Alice", "allowed": True}]})
    assert "status" not in c.senders["+15551234567"]


def test_to_dict_emits_list_shape_sorted_by_phone():
    """to_dict emits senders as a list of {phone, name, allowed} dicts, sorted by phone."""
    c = SignConfig(
        senders={
            "+15559999999": {"name": "Bob", "allowed": False, "phone": "+15559999999"},
            "+15551234567": {"name": "Alice", "allowed": True, "phone": "+15551234567"},
        }
    )
    d = c.to_dict()
    assert d["senders"] == [
        {"phone": "+15551234567", "name": "Alice", "allowed": True},
        {"phone": "+15559999999", "name": "Bob", "allowed": False},
    ]


def test_to_dict_includes_all_v3_blocks():
    """to_dict includes sign_settings, effects_settings (with name_display_format),
    and text_settings (with enforcement_enabled)."""
    c = SignConfig()
    d = c.to_dict()
    assert "sign_settings" in d
    assert d["text_settings"]["enforcement_enabled"] is True
    assert d["effects_settings"]["name_display_format"] == "first_initial_if_duplicates"


def test_round_trip_senders_preserves_original_phone():
    """from_dict(to_dict(cfg)) preserves the ORIGINAL phone format (not normalized)."""
    c = SignConfig(
        senders={
            "+15551234567": {
                "name": "Alice",
                "allowed": True,
                "phone": "+1 (555) 123-4567",
            }
        }
    )
    d = c.to_dict()
    # Round-tripping via dict-of-dict normalizes the lookup key but
    # the `phone` field on the value preserves the ORIGINAL.
    c2 = SignConfig.from_dict(d)
    assert c2.senders["+15551234567"]["phone"] == "+1 (555) 123-4567"
    assert c2.senders["+15551234567"]["allowed"] is True


def test_sign_config_rejects_legacy_allowed_senders_kwarg():
    """SignConfig(allowed_senders=[...]) raises TypeError (parameter removed)."""
    with pytest.raises(TypeError):
        SignConfig(allowed_senders=["+15551234567"])  # type: ignore[call-arg]


def test_from_dict_rejects_unknown_name_display_format():
    """from_dict raises ValueError on an unknown name_display_format value."""
    with pytest.raises(ValueError):
        SignConfig.from_dict({"effects_settings": {"name_display_format": "last_only"}})


def test_from_dict_accepts_valid_name_display_format():
    """from_dict accepts the four valid format values."""
    for fmt in ("full", "first_initial", "first", "first_initial_if_duplicates"):
        c = SignConfig.from_dict({"effects_settings": {"name_display_format": fmt}})
        assert c.effects_settings.name_display_format == fmt


def test_from_dict_rejects_non_bool_enforcement_enabled():
    """from_dict raises ValueError when enforcement_enabled is not a bool."""
    with pytest.raises(ValueError):
        SignConfig.from_dict({"text_settings": {"enforcement_enabled": "yes"}})


# --- should_render_sender scenarios (Section 5.5) ---


def test_should_render_sender_allowed_true_returns_true():
    """allowed=True + enforcement on → True."""
    senders = {"+15551234567": {"name": "Alice", "allowed": True, "phone": "+15551234567"}}
    assert should_render_sender("+15551234567", senders, True) is True


def test_should_render_sender_allowed_false_returns_false():
    """allowed=False + enforcement on → False."""
    senders = {"+15551234567": {"name": "Alice", "allowed": False, "phone": "+15551234567"}}
    assert should_render_sender("+15551234567", senders, True) is False


def test_should_render_sender_unlisted_returns_false():
    """Sender NOT in dict + enforcement on → False (allowlist is exclusive)."""
    assert should_render_sender("+15551234567", {}, True) is False


def test_should_render_sender_enforcement_off_bypasses_allowed_false():
    """Master toggle off → True regardless of per-entry allowed=False."""
    senders = {"+15551234567": {"name": "Alice", "allowed": False, "phone": "+15551234567"}}
    assert should_render_sender("+15551234567", senders, False) is True


def test_should_render_sender_enforcement_off_unlisted_still_renders():
    """Master toggle off → True even for unlisted senders."""
    assert should_render_sender("+15551234567", {}, False) is True


def test_should_render_sender_normalizes_phone_formats():
    """Lookup normalizes phone formats via phone_utils.normalize_phone."""
    senders = {"+15551234567": {"name": "Alice", "allowed": True, "phone": "+15551234567"}}
    for fmt in ("+1 (555) 123-4567", "5551234567", "555-123-4567"):
        assert should_render_sender(fmt, senders, True) is True


# --- End-to-end through MessageManager (Section 5.5) ---


def test_message_manager_drops_disallowed_sender_when_enforcement_on():
    """A disallowed sender's MessageView ends up with suppressed=True (synthetic marker)."""
    from lib_shared.message_manager import MessageManager

    mgr = MessageManager(
        messages_api_url="http://x/api/messages",
        config_api_url="http://x/api/config",
        api_key="key",
    )
    # Disallow the sender
    mgr.config.senders["+15551234567"] = {
        "name": "Block",
        "allowed": False,
        "phone": "+15551234567",
    }
    mgr._handle_message(
        {
            "id": "m1",
            "sender": "+15551234567",
            "body": "hi",
            "received_at": "2026-06-01T12:00:00Z",
        }
    )
    msgs = mgr.get_messages(limit=10, suppress=False)
    assert len(msgs) == 1
    assert msgs[0].suppressed is True
    # Synthetic `sender_action` marker added (no FilterRule matched)
    assert msgs[0].rules and msgs[0].rules[0]["type"] == "sender_action"


def test_message_manager_renders_allowed_sender_when_enforcement_on():
    """An allowed sender renders normally (no synthetic marker)."""
    from lib_shared.message_manager import MessageManager

    mgr = MessageManager(
        messages_api_url="http://x/api/messages",
        config_api_url="http://x/api/config",
        api_key="key",
    )
    mgr.config.senders["+15551234567"] = {
        "name": "Alice",
        "allowed": True,
        "phone": "+15551234567",
    }
    mgr._handle_message(
        {
            "id": "m1",
            "sender": "+15551234567",
            "body": "hi",
            "received_at": "2026-06-01T12:00:00Z",
        }
    )
    msgs = mgr.get_messages(limit=10, suppress=False)
    assert msgs[0].suppressed is False
    assert msgs[0].rules == []


def test_message_manager_enforcement_off_bypasses_senders_allowlist():
    """When enforcement_enabled=False, every sender renders regardless of senders list."""
    from lib_shared.message_manager import MessageManager

    mgr = MessageManager(
        messages_api_url="http://x/api/messages",
        config_api_url="http://x/api/config",
        api_key="key",
    )
    # No senders entries — enforcement off → every sender renders
    mgr.config.text_settings.enforcement_enabled = False
    mgr._handle_message(
        {
            "id": "m1",
            "sender": "+15559999999",
            "body": "hi",
            "received_at": "2026-06-01T12:00:00Z",
        }
    )
    msgs = mgr.get_messages(limit=10, suppress=False)
    assert msgs[0].suppressed is False
