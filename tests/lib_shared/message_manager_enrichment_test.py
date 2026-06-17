"""Tests for pre-event enrichment of MessageView in lib_shared.messages / message_manager.

Scenarios:
1. New message arrival enriches only the new entry (existing entries' derived fields unchanged).
2. Config change re-enriches all entries (mutation of an existing MessageView's suppressed after
   a filter rule is added).
3. get_messages() does not invoke _apply_filter or _matches (spy on them, assert no calls during read).
4. get_messages() does not invoke _format_display_time (spy, assert no calls during read).
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from lib_shared.messages import InMemoryMessages
from lib_shared.message_manager import MessageManager
from lib_shared.models import EffectsSettings, FilterRule, Message, SignConfig, TextSettings


def _make_config(timezone="America/Los_Angeles", filters=None, senders=None):
    return SignConfig(
        effect_settings=EffectsSettings(),
        text_settings=TextSettings(),
        timezone=timezone,
        filters=list(filters or []),
        senders=dict(senders or {}),
    )


# --- Scenario 1: new message arrival enriches only the new entry --------------


def test_new_message_arrival_enriches_only_new_entry():
    """When a new message is added, only the new MessageView's derived fields are populated.
    Existing entries in the buffer keep their prior derived state (untouched)."""
    cfg = _make_config()
    store = InMemoryMessages(cfg, maxlen=10)
    store.add(Message(id="a", sender="+1", body="first", received_at="2026-01-01T00:00:00Z"))

    # Snapshot the existing entry's derived fields (should be already-enriched by add).
    existing_view = list(store._msgs)[0]
    existing_suppressed_before = existing_view.suppressed
    existing_rules_before = existing_view.rules
    existing_sender_name_before = existing_view.sender_name
    existing_display_time_before = existing_view.display_time

    # Add a second message — the new entry must be enriched but the old one must not be touched.
    store.add(Message(id="b", sender="+1", body="second", received_at="2026-01-02T00:00:00Z"))

    # The existing entry's derived fields are exactly the same.
    assert existing_view.suppressed == existing_suppressed_before
    assert existing_view.rules == existing_rules_before
    assert existing_view.sender_name == existing_sender_name_before
    assert existing_view.display_time == existing_display_time_before

    # The new entry's derived fields are populated.
    new_view = [v for v in store._msgs if v.message.id == "b"][0]
    assert new_view.suppressed is False
    assert new_view.rules == []
    assert new_view.sender_name is None
    # display_time is populated (not None) when the timezone parses the timestamp.
    assert new_view.display_time is not None and "2026" in new_view.display_time


# --- Scenario 2: config change re-enriches all entries -------------------------


def test_config_change_re_enriches_all_entries():
    """After a config change adds a filter rule, existing entries' `suppressed` flips.

    The mutation happens because `re_enrich_all()` re-runs `_enrich_messages`
    on the SAME MessageView instances stored in the deque — so callers
    holding references see the updated fields.
    """
    cfg = _make_config()
    store = InMemoryMessages(cfg, maxlen=10)
    store.add(Message(id="a", sender="+1", body="hello world", received_at="2026-01-01T00:00:00Z"))
    store.add(Message(id="b", sender="+1", body="bad news", received_at="2026-01-02T00:00:00Z"))

    # Hold a reference to the 'b' MessageView instance.
    b_view = [v for v in store._msgs if v.message.id == "b"][0]
    assert b_view.suppressed is False

    # Add a filter rule that catches "b" but not "a", then re-enrich.
    cfg.filters.append(FilterRule(type="keyword", pattern="bad", action="suppress"))
    store.re_enrich_all()

    # The previously-stored MessageView now reports suppressed=True (mutated in place).
    assert b_view.suppressed is True
    assert b_view.rules and b_view.rules[0]["pattern"] == "bad"

    # 'a' was not affected by the new rule.
    a_view = [v for v in store._msgs if v.message.id == "a"][0]
    assert a_view.suppressed is False


# --- Scenario 3: get_messages does not invoke _apply_filter / _matches --------


def test_get_messages_does_not_invoke_filter_logic():
    """`get_messages()` is a thin read on the hot path: it must not invoke _apply_filter or _matches."""
    cfg = _make_config(filters=[FilterRule(type="keyword", pattern="bad", action="suppress")])
    store = InMemoryMessages(cfg, maxlen=10)
    store.add(Message(id="a", sender="+1", body="hello", received_at="2026-01-01T00:00:00Z"))
    store.add(Message(id="b", sender="+1", body="bad news", received_at="2026-01-02T00:00:00Z"))

    with patch.object(store, "_apply_filter", wraps=store._apply_filter) as apply_spy, \
         patch.object(store, "_matches", wraps=store._matches) as matches_spy:
        out = store.get_messages(limit=10, suppress=True)
    assert len(out) == 1
    assert out[0].message.id == "a"
    apply_spy.assert_not_called()
    matches_spy.assert_not_called()

    # Also for suppress=False.
    with patch.object(store, "_apply_filter", wraps=store._apply_filter) as apply_spy, \
         patch.object(store, "_matches", wraps=store._matches) as matches_spy:
        out_all = store.get_messages(limit=10, suppress=False)
    assert len(out_all) == 2
    apply_spy.assert_not_called()
    matches_spy.assert_not_called()


# --- Scenario 4: get_messages does not invoke _format_display_time --------------


def test_get_messages_does_not_invoke_format_display_time():
    """`get_messages()` does not call the timezone formatter on the read path."""
    from lib_shared import messages as messages_mod

    cfg = _make_config()
    store = InMemoryMessages(cfg, maxlen=10)
    store.add(Message(id="a", sender="+1", body="hello", received_at="2026-01-01T00:00:00Z"))

    with patch.object(messages_mod, "_format_display_time", wraps=messages_mod._format_display_time) as fmt_spy:
        out = store.get_messages(limit=10, suppress=True)
    assert len(out) == 1
    # display_time was populated on add() (write-time), so it is present without re-formatting.
    assert out[0].display_time is not None
    fmt_spy.assert_not_called()


# --- Bonus: MessageManager._handle_config triggers re_enrich_all -----------------


def test_message_manager_handle_config_re_enriches():
    """`MessageManager._handle_config()` calls `re_enrich_all()` on the messages store.

    Verifies the manager-level wiring so the event-time re-enrich happens
    for an envelope-driven config update (not just a direct call).
    """
    mgr = MessageManager(
        messages_api_url="http://localhost/api/messages",
        config_api_url="http://localhost/api/config",
        api_key="key",
    )
    # Seed a message before the config change.
    mgr._handle_message(
        {
            "id": "a",
            "sender": "+1",
            "body": "bad news",
            "received_at": "2026-01-01T00:00:00Z",
        }
    )
    a_view = list(mgr.messages._msgs)[0]
    assert a_view.suppressed is False

    # Push a config that adds a 'bad' keyword filter.
    mgr._handle_config(
        {
            "filters": [{"type": "keyword", "pattern": "bad", "action": "suppress"}],
            "senders": [],
            "effect_settings": {"effects": [{"name": "Hyperspace", "enabled": True}]},
            "text_settings": {"speed": 3, "color": 16711680, "text_effect": "scroll"},
            "sign": {"name": "Lindsay's Heart"},
            "timezone": "US/Pacific",
            "version": 2,
        }
    )
    # The existing entry is now suppressed (re-enrich ran).
    assert a_view.suppressed is True