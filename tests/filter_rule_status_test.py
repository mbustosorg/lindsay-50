"""Tests for the v3 FilterRule.status field + restricted type/action whitelists.

Covers:
- Default status="enabled"
- from_dict accepts missing status (back-compat default)
- from_dict accepts explicit status="disabled"
- from_dict rejects unknown action values
- from_dict rejects type="sender" (REMOVED from wire in v3)
- to_dict always includes status
- _apply_filter skips disabled rules
"""

import pytest

from lib_shared.messages import InMemoryMessages
from lib_shared.models import FilterRule, Message, SignConfig

# --- Status field (Section 3.5) ---


def test_default_status_is_enabled():
    """FilterRule(...) defaults status='enabled'."""
    r = FilterRule(type="keyword", pattern="spam")
    assert r.status == "enabled"


def test_from_dict_missing_status_defaults_enabled():
    """from_dict without `status` key → 'enabled' (back-compat default)."""
    r = FilterRule.from_dict({"type": "keyword", "pattern": "spam"})
    assert r.status == "enabled"


def test_from_dict_explicit_disabled():
    """from_dict with status='disabled' is preserved."""
    r = FilterRule.from_dict({"type": "keyword", "pattern": "spam", "status": "disabled"})
    assert r.status == "disabled"


def test_from_dict_rejects_unknown_status():
    """from_dict rejects unknown status values."""
    with pytest.raises(ValueError):
        FilterRule.from_dict({"type": "keyword", "pattern": "spam", "status": "paused"})


def test_from_dict_rejects_unknown_action():
    """from_dict rejects action='allow' (not in v1's allowed set)."""
    with pytest.raises(ValueError):
        FilterRule.from_dict({"type": "keyword", "pattern": "spam", "action": "allow"})


def test_from_dict_rejects_sender_type():
    """from_dict rejects type='sender' — REMOVED from wire in v3."""
    with pytest.raises(ValueError):
        FilterRule.from_dict({"type": "sender", "pattern": "+15551234567"})


def test_from_dict_rejects_unknown_type():
    """from_dict rejects unknown type values."""
    with pytest.raises(ValueError):
        FilterRule.from_dict({"type": "user", "pattern": "x"})


def test_to_dict_always_includes_status():
    """to_dict always emits the status key (no conditional omission)."""
    r = FilterRule(type="keyword", pattern="spam", action="suppress", status="disabled")
    d = r.to_dict()
    assert d["status"] == "disabled"


# --- _apply_filter skip-disabled behavior ---


def _make_store_with_filter(status="enabled"):
    cfg = SignConfig(
        filters=[FilterRule(type="keyword", pattern="bad", action="suppress", status=status)],
        # Allowlist the test sender so the senders list doesn't suppress.
        senders={"+1": {"name": "X", "allowed": True, "phone": "+1"}},
    )
    return cfg, InMemoryMessages(cfg, maxlen=10)


def test_apply_filter_skips_disabled_rules():
    """A disabled FilterRule is skipped by _apply_filter (treated as absent)."""
    cfg, store = _make_store_with_filter(status="disabled")
    msg = Message(id="m1", sender="+1", body="has bad word", received_at="2026-01-01T00:00:00Z")
    store.add(msg)
    store._enrich_messages(list(store._msgs))
    out = store.get_messages(limit=10, suppress=False)
    assert len(out) == 1
    assert out[0].suppressed is False
    assert out[0].rules == []


def test_apply_filter_enabled_rule_suppresses():
    """An enabled FilterRule applies (the suppression happens)."""
    cfg, store = _make_store_with_filter(status="enabled")
    msg = Message(id="m1", sender="+1", body="has bad word", received_at="2026-01-01T00:00:00Z")
    store.add(msg)
    store._enrich_messages(list(store._msgs))
    out = store.get_messages(limit=10, suppress=False)
    assert len(out) == 1
    assert out[0].suppressed is True
    assert out[0].rules[0]["type"] == "keyword"
