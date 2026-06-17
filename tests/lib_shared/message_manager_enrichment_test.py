"""Tests for pre-event enrichment of MessageView in lib_shared.messages / message_manager.

Scenarios:
1. `MessageManager._handle_message` enriches only the new entry (existing
   entries' derived fields are unchanged).
2. `MessageManager._handle_config` re-enriches all entries (mutation of
   an existing MessageView's `suppressed` after a filter rule is added).
3. `get_messages()` does not invoke `_apply_filter` or `_matches` (spy
   on them, assert no calls during read).
4. `get_messages()` does not invoke `_format_display_time` (spy, assert
   no calls during read).
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


# --- Scenario 1: _handle_message enriches only the new entry -----------------


def test_handle_message_enriches_only_new_entry():
    """When `_handle_message` receives a new message, only the new
    MessageView's derived fields are populated. Existing entries in
    the buffer keep their prior derived state (untouched)."""
    mgr = MessageManager(
        messages_api_url="http://localhost/api/messages",
        config_api_url="http://localhost/api/config",
        api_key="key",
    )
    mgr._handle_message(
        {
            "id": "a",
            "sender": "+1",
            "body": "first",
            "received_at": "2026-01-01T00:00:00Z",
        }
    )
    # The first entry is enriched by _handle_message.
    existing_view = list(mgr.messages._msgs)[0]
    assert existing_view.suppressed is False
    assert existing_view.rules == []
    assert existing_view.display_time is not None
    # Hold derived fields for the equality check.
    snap = (
        existing_view.suppressed,
        list(existing_view.rules),
        existing_view.sender_name,
        existing_view.display_time,
    )

    # Route a second message through _handle_message.
    mgr._handle_message(
        {
            "id": "b",
            "sender": "+1",
            "body": "second",
            "received_at": "2026-01-02T00:00:00Z",
        }
    )

    # The existing entry's derived fields are exactly the same.
    assert existing_view.suppressed == snap[0]
    assert list(existing_view.rules) == snap[1]
    assert existing_view.sender_name == snap[2]
    assert existing_view.display_time == snap[3]

    # The new entry's derived fields are populated.
    new_view = [v for v in mgr.messages._msgs if v.message.id == "b"][0]
    assert new_view.suppressed is False
    assert new_view.rules == []
    assert new_view.display_time is not None and "2026" in new_view.display_time


def test_handle_message_skips_enrich_on_duplicate():
    """A duplicate id is silently dropped by `InMemoryMessages.add` —
    `_handle_message` sees the `None` return and skips the
    `_enrich_messages` call (no spurious work, no extra change event)."""
    mgr = MessageManager(
        messages_api_url="http://localhost/api/messages",
        config_api_url="http://localhost/api/config",
        api_key="key",
    )
    payload = {
        "id": "a",
        "sender": "+1",
        "body": "first",
        "received_at": "2026-01-01T00:00:00Z",
    }
    mgr._handle_message(payload)
    # Second call with the same id: add() returns None, manager
    # skips the enrich call. Buffer is unchanged.
    with patch.object(mgr.messages, "_enrich_messages", wraps=mgr.messages._enrich_messages) as enrich_spy:
        mgr._handle_message(payload)
    enrich_spy.assert_not_called()
    assert len(mgr.messages._msgs) == 1


# --- Scenario 2: _handle_config re-enriches all entries ----------------------


def test_handle_config_re_enriches_all_entries():
    """After a config change adds a filter rule, existing entries'
    `suppressed` flips. The mutation happens because `_handle_config`
    calls `_enrich_messages` on the same MessageView instances
    stored in the deque — callers holding references see the
    updated fields."""
    mgr = MessageManager(
        messages_api_url="http://localhost/api/messages",
        config_api_url="http://localhost/api/config",
        api_key="key",
    )
    mgr._handle_message(
        {
            "id": "a",
            "sender": "+1",
            "body": "hello world",
            "received_at": "2026-01-01T00:00:00Z",
        }
    )
    mgr._handle_message(
        {
            "id": "b",
            "sender": "+1",
            "body": "bad news",
            "received_at": "2026-01-02T00:00:00Z",
        }
    )

    # Hold a reference to the 'b' MessageView instance.
    b_view = [v for v in mgr.messages._msgs if v.message.id == "b"][0]
    assert b_view.suppressed is False

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
    # The previously-stored MessageView now reports suppressed=True
    # (mutated in place by the re-enrich).
    assert b_view.suppressed is True
    assert b_view.rules and b_view.rules[0]["pattern"] == "bad"

    # 'a' was not affected by the new rule.
    a_view = [v for v in mgr.messages._msgs if v.message.id == "a"][0]
    assert a_view.suppressed is False


def test_handle_config_enriches_whole_buffer_on_call():
    """`_handle_config` always calls `_enrich_messages` with the full
    buffer (single whole-list pass), not per-entry. Spy and assert
    the call shape."""
    mgr = MessageManager(
        messages_api_url="http://localhost/api/messages",
        config_api_url="http://localhost/api/config",
        api_key="key",
    )
    for i in range(3):
        mgr._handle_message(
            {
                "id": f"m{i}",
                "sender": "+1",
                "body": f"body-{i}",
                "received_at": f"2026-01-0{i + 1}T00:00:00Z",
            }
        )
    with patch.object(mgr.messages, "_enrich_messages", wraps=mgr.messages._enrich_messages) as enrich_spy:
        mgr._handle_config(
            {
                "filters": [],
                "senders": [],
                "effect_settings": {"effects": []},
                "text_settings": {"speed": 3, "color": 16711680, "text_effect": "scroll"},
                "sign": {"name": "x"},
                "timezone": "US/Pacific",
                "version": 2,
            }
        )
    # Exactly one call, with the full buffer as the argument.
    assert enrich_spy.call_count == 1
    args, _ = enrich_spy.call_args
    assert len(args[0]) == 3


# --- Scenario 3: get_messages does not invoke _apply_filter / _matches --------


def test_get_messages_does_not_invoke_filter_logic():
    """`get_messages()` is a thin read on the hot path: it must not
    invoke `_apply_filter` or `_matches`."""
    cfg = _make_config(filters=[FilterRule(type="keyword", pattern="bad", action="suppress")])
    store = InMemoryMessages(cfg, maxlen=10)
    # Two entries with mixed suppressed / non-suppressed — the buffer
    # is pre-enriched at construction time. (In production this is the
    # manager's job; here we drive `_enrich_messages` directly so the
    # test exercises only the read path.)
    store.add(Message(id="a", sender="+1", body="hello", received_at="2026-01-01T00:00:00Z"))
    store.add(Message(id="b", sender="+1", body="bad news", received_at="2026-01-02T00:00:00Z"))
    store._enrich_messages(list(store._msgs))

    with (
        patch.object(store, "_apply_filter", wraps=store._apply_filter) as apply_spy,
        patch.object(store, "_matches", wraps=store._matches) as matches_spy,
    ):
        out = store.get_messages(limit=10, suppress=True)
    assert len(out) == 1
    assert out[0].message.id == "a"
    apply_spy.assert_not_called()
    matches_spy.assert_not_called()

    with (
        patch.object(store, "_apply_filter", wraps=store._apply_filter) as apply_spy,
        patch.object(store, "_matches", wraps=store._matches) as matches_spy,
    ):
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
    store._enrich_messages(list(store._msgs))

    with patch.object(messages_mod, "_format_display_time", wraps=messages_mod._format_display_time) as fmt_spy:
        out = store.get_messages(limit=10, suppress=True)
    assert len(out) == 1
    # display_time was populated on the add+enrich, so it is present
    # without re-formatting.
    assert out[0].display_time is not None
    fmt_spy.assert_not_called()
