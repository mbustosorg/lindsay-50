"""Tests for the senders-status capability (tasks 2.5 + 4.5).

Covers the SignConfig.senders dict-of-dict shape, the should_render_sender
egress decision, and end-to-end allow/suppress/enabled/disabled behavior
through MessageManager (including the egress-not-ingress re-enrich guarantee
and the synthetic sender_action marker).
"""

import pytest

from lib_shared.messages import should_render_sender
from lib_shared.message_manager import MessageManager
from lib_shared.models import SignConfig

# ---------------------------------------------------------------------------
# Task 2.5 — model shape
# ---------------------------------------------------------------------------


def test_default_senders_is_empty_dict():
    assert SignConfig().senders == {}


def test_from_dict_full_entry_normalizes_key_preserves_original():
    cfg = SignConfig.from_dict(
        {
            "version": 3,
            "senders": [{"phone": "+15551234567", "name": "Alice", "action": "allow", "status": "enabled"}],
        }
    )
    assert cfg.senders["+15551234567"] == {
        "name": "Alice",
        "action": "allow",
        "status": "enabled",
        "phone": "+15551234567",
    }


def test_from_dict_missing_action_status_defaults():
    cfg = SignConfig.from_dict({"version": 3, "senders": [{"phone": "+15551234567", "name": "Alice"}]})
    assert cfg.senders["+15551234567"]["action"] == "allow"
    assert cfg.senders["+15551234567"]["status"] == "enabled"


def test_to_dict_emits_sorted_list_with_action_status():
    cfg = SignConfig(
        version=3,
        senders={
            "+15559999999": {
                "name": "Zoe",
                "action": "suppress",
                "status": "disabled",
                "phone": "+15559999999",
            },
            "+15551234567": {
                "name": "Alice",
                "action": "allow",
                "status": "enabled",
                "phone": "+15551234567",
            },
        },
    )
    wire = cfg.to_dict()["senders"]
    assert wire == [
        {"phone": "+15551234567", "name": "Alice", "action": "allow", "status": "enabled"},
        {"phone": "+15559999999", "name": "Zoe", "action": "suppress", "status": "disabled"},
    ]


def test_round_trip_preserves_original_phone_and_fields():
    cfg = SignConfig(
        version=3,
        senders={
            "+15551234567": {
                "name": "Alice",
                "action": "suppress",
                "status": "disabled",
                "phone": "+1 (555) 123-4567",
            }
        },
    )
    cfg2 = SignConfig.from_dict(cfg.to_dict())
    entry = cfg2.senders["+15551234567"]
    assert entry["phone"] == "+1 (555) 123-4567"
    assert entry["action"] == "suppress"
    assert entry["status"] == "disabled"
    assert entry["name"] == "Alice"


def test_allowed_senders_kwarg_removed():
    with pytest.raises(TypeError):
        SignConfig(allowed_senders=["+15551234567"])


# ---------------------------------------------------------------------------
# Task 4.5 — should_render_sender
# ---------------------------------------------------------------------------


def _entry(action="allow", status="enabled", phone="+15551234567", name="Alice"):
    return {"name": name, "action": action, "status": status, "phone": phone}


def test_should_render_allow_enabled_true():
    assert should_render_sender("+15551234567", {"+15551234567": _entry()}) is True


def test_should_render_allow_disabled_false():
    assert should_render_sender("+15551234567", {"+15551234567": _entry(status="disabled")}) is False


def test_should_render_suppress_enabled_false():
    assert should_render_sender("+15551234567", {"+15551234567": _entry(action="suppress")}) is False


def test_should_render_suppress_disabled_false():
    assert should_render_sender("+15551234567", {"+15551234567": _entry(action="suppress", status="disabled")}) is False


def test_should_render_unlisted_false():
    assert should_render_sender("+15551234567", {}) is False


def test_should_render_normalizes_incoming_sender():
    assert should_render_sender("+1 (555) 123-4567", {"+15551234567": _entry()}) is True


# ---------------------------------------------------------------------------
# Task 4.5 — end-to-end through MessageManager
# ---------------------------------------------------------------------------

_ALICE = "+15551234567"


def _mgr():
    return MessageManager(
        messages_api_url="http://localhost/api/messages",
        config_api_url="http://localhost/api/config",
        api_key="key",
    )


def _push_config(mgr, senders_wire, filters_wire=None):
    """Push a v3 config (no migration mangling) and re-enrich the buffer."""
    mgr._handle_config(
        {
            "version": 3,
            "senders": senders_wire,
            "filters": filters_wire or [],
        }
    )


def _sms(mgr, body="hello", sender=_ALICE, msg_id="m1"):
    mgr._handle_message({"id": msg_id, "sender": sender, "body": body, "received_at": "2026-06-01T12:00:00Z"})


def test_end_to_end_allow_enabled_renders():
    mgr = _mgr()
    _push_config(mgr, [{"phone": _ALICE, "name": "Alice", "action": "allow", "status": "enabled"}])
    _sms(mgr)
    kept = mgr.get_messages(suppress=True)
    assert [m.message.id for m in kept] == ["m1"]


def test_end_to_end_allow_disabled_suppressed():
    mgr = _mgr()
    _push_config(mgr, [{"phone": _ALICE, "name": "Alice", "action": "allow", "status": "disabled"}])
    _sms(mgr)
    assert mgr.get_messages(suppress=True) == []
    assert len(mgr.get_messages(suppress=False)) == 1  # stored on ingress


def test_end_to_end_suppress_enabled_suppressed():
    mgr = _mgr()
    _push_config(mgr, [{"phone": _ALICE, "name": "Alice", "action": "suppress", "status": "enabled"}])
    _sms(mgr)
    assert mgr.get_messages(suppress=True) == []
    assert len(mgr.get_messages(suppress=False)) == 1


def test_end_to_end_unlisted_suppressed():
    mgr = _mgr()
    _push_config(mgr, [])
    _sms(mgr)
    assert mgr.get_messages(suppress=True) == []
    assert len(mgr.get_messages(suppress=False)) == 1


def test_config_update_adds_sender_flips_visible():
    """Egress-not-ingress: adding a sender later un-suppresses stored messages."""
    mgr = _mgr()
    _push_config(mgr, [])  # Alice unlisted
    _sms(mgr)
    assert mgr.get_messages(suppress=True) == []
    # Now add Alice as allow + enabled — no Twilio re-fetch.
    _push_config(mgr, [{"phone": _ALICE, "name": "Alice", "action": "allow", "status": "enabled"}])
    assert [m.message.id for m in mgr.get_messages(suppress=True)] == ["m1"]


def test_config_update_flip_enabled_to_disabled_suppresses():
    mgr = _mgr()
    _push_config(mgr, [{"phone": _ALICE, "name": "Alice", "action": "allow", "status": "enabled"}])
    _sms(mgr)
    assert len(mgr.get_messages(suppress=True)) == 1
    _push_config(mgr, [{"phone": _ALICE, "name": "Alice", "action": "allow", "status": "disabled"}])
    assert mgr.get_messages(suppress=True) == []


def test_config_update_flip_allow_to_suppress_suppresses():
    mgr = _mgr()
    _push_config(mgr, [{"phone": _ALICE, "name": "Alice", "action": "allow", "status": "enabled"}])
    _sms(mgr)
    assert len(mgr.get_messages(suppress=True)) == 1
    _push_config(mgr, [{"phone": _ALICE, "name": "Alice", "action": "suppress", "status": "enabled"}])
    assert mgr.get_messages(suppress=True) == []


def test_synthetic_sender_action_marker_when_no_filter_matched():
    mgr = _mgr()
    _push_config(mgr, [])  # unlisted → suppressed by senders
    _sms(mgr, body="totally clean")
    entry = mgr.get_messages(suppress=False)[0]
    assert entry.suppressed is True
    assert entry.rules == [{"type": "sender_action", "pattern": _ALICE, "action": "suppress"}]


def test_no_synthetic_marker_when_filter_also_matched():
    mgr = _mgr()
    _push_config(
        mgr,
        [],  # unlisted → suppressed by senders
        filters_wire=[{"type": "keyword", "pattern": "spam", "action": "suppress"}],
    )
    _sms(mgr, body="this is spam")
    entry = mgr.get_messages(suppress=False)[0]
    assert entry.suppressed is True
    # The real keyword rule wins; no synthetic sender_action marker.
    assert [r["type"] for r in entry.rules] == ["keyword"]


def test_sender_name_resolves_regardless_of_action_status():
    mgr = _mgr()
    _push_config(mgr, [{"phone": _ALICE, "name": "Alice", "action": "suppress", "status": "disabled"}])
    _sms(mgr)
    entry = mgr.get_messages(suppress=False)[0]
    assert entry.suppressed is True
    assert entry.sender_name == "Alice"
