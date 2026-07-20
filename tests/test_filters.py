"""Tests for filter logic in lib_shared/messages.py (FilteredMessages).

v3 (issue #6): sender-type FilterRule is REMOVED — sender matching lives in
the `cfg.senders` allowlist (`allowed: bool` per entry + master
`sign_settings.enforce_allowed_senders` toggle). The `test_sender_*` cases here
exercise the v3 senders list path via `_enrich_messages`, not the
`type="sender"` branch in `_matches`.
"""

import pytest
from lib_shared.messages import InMemoryMessages, should_render_sender
from lib_shared.models import FilterRule, Message, SignConfig

# ---------------------------------------------------------------------------
# Helpers matching the original test API surface
# ---------------------------------------------------------------------------


def apply(msg, cfg):
    """Return (suppressed: bool, first_matching_rule: FilterRule or None).

    Uses `_enrich_messages` so both FilterRule matching and the senders
    allowlist apply. A message is suppressed if EITHER a FilterRule matched
    OR the senders list said no.

    `first_matching_rule` is returned as a FilterRule instance when one
    matched (by mapping the entry.rules[0] dict back to the rule object
    via pattern+type lookup). For synthetic `sender_action` markers,
    returns None — there's no FilterRule to return.
    """
    fm = InMemoryMessages(cfg, maxlen=100)
    fm.add(msg)
    fm._enrich_messages(list(fm._msgs))
    view = fm.get_messages(limit=1, suppress=False)
    if not view:
        return False, None
    entry = view[0]
    if not entry.suppressed:
        return False, None
    if not entry.rules:
        return True, None
    first = entry.rules[0]
    if not isinstance(first, dict):
        return True, first
    # It's a dict (either a real rule's to_dict() or a sender_action marker).
    if first.get("type") == "sender_action":
        return True, None
    # Map the dict back to a FilterRule object so callers can introspect.
    matched = FilterRule(
        type=first.get("type") or "keyword",
        pattern=first.get("pattern") or "",
        action=first.get("action", "suppress"),
    )
    return True, matched


def get_messages(msgs, cfg, include_filtered=False, since=None):
    """Filter msgs by cfg rules, return newest-first list.

    Mirrors the original test API surface using InMemoryMessages.
    In production the MessageManager drives enrichment at event time;
    here we call `_enrich_messages` after the add loop so the buffer
    is fully enriched before the read.
    """
    store = InMemoryMessages(cfg, maxlen=100)
    for m in msgs:
        store.add(m)
    store._enrich_messages(list(store._msgs))
    result = store.get_messages(limit=100, suppress=not include_filtered)
    if since is not None:
        result = [e for e in result if e.message.received_at > since]
    if include_filtered:
        return [{"message": e.message, "suppressed": e.suppressed, "rules": e.rules} for e in result]
    return [e.message for e in result]


# ---------------------------------------------------------------------------
# Sample data fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_messages():
    return [
        Message(
            id="msg-001",
            sender="+15551234567",
            body="Hello world",
            received_at="2026-05-08T10:00:00Z",
        ),
        Message(
            id="msg-002",
            sender="+15559876543",
            body="badword inside",
            received_at="2026-05-08T11:00:00Z",
        ),
        Message(
            id="msg-003",
            sender="+15550001111",
            body="Another message",
            received_at="2026-05-08T12:00:00Z",
        ),
    ]


@pytest.fixture
def default_config():
    return SignConfig(
        version=3,
        filters=[],
        senders={},
    )


@pytest.fixture
def config_with_keyword_filter():
    return SignConfig(
        version=3,
        filters=[FilterRule(type="keyword", pattern="badword", action="suppress")],
        senders={},
    )


@pytest.fixture
def config_with_sender_filter():
    """v3: sender suppression via the senders allowlist (allowed=False).

    `type="sender"` FilterRule is REMOVED. The senders list is the
    single source of truth for sender matching.
    """
    return SignConfig(
        version=3,
        filters=[],
        senders={"+15550001111": {"name": "Block", "allowed": False, "phone": "+15550001111"}},
    )


@pytest.fixture
def config_with_regex_filter():
    return SignConfig(
        version=3,
        filters=[FilterRule(type="regex", pattern=r"^\s*$", action="suppress")],
        senders={},
    )


@pytest.fixture
def config_with_message_filter():
    return SignConfig(
        version=3,
        filters=[FilterRule(type="message", pattern="msg-002", action="suppress")],
        senders={},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestApply:
    def test_no_filters_passes(self, sample_messages, default_config):
        # With senders={} and enforce_allowed_senders=True (default), every
        # sender is unlisted → senders list suppresses everything. Add
        # explicit allowlist entries so the sample messages render.
        for m in sample_messages:
            default_config.senders[m.sender] = {"name": "X", "allowed": True, "phone": m.sender}
            suppressed, rule = apply(m, default_config)
            assert not suppressed
            assert rule is None

    def test_keyword_suppress_case_insensitive(self, default_config):
        default_config.filters.append(FilterRule(type="keyword", pattern="BADWORD", action="suppress"))
        # Add a sender entry so the senders list lets the message through
        default_config.senders["+1555"] = {"name": "X", "allowed": True, "phone": "+1555"}
        msg = Message(id="1", sender="+1555", body="has badword in it", received_at="")
        suppressed, rule = apply(msg, default_config)
        assert suppressed
        assert rule is not None
        assert rule.type == "keyword"
        assert rule.pattern == "BADWORD"

    def test_keyword_no_match(self, default_config):
        default_config.filters.append(FilterRule(type="keyword", pattern="badword", action="suppress"))
        default_config.senders["+1555"] = {"name": "X", "allowed": True, "phone": "+1555"}
        msg = Message(id="1", sender="+1555", body="clean message", received_at="")
        suppressed, rule = apply(msg, default_config)
        assert not suppressed

    def test_regex_suppress(self, config_with_regex_filter):
        config_with_regex_filter.senders["+1555"] = {"name": "X", "allowed": True, "phone": "+1555"}
        msg = Message(id="1", sender="+1555", body="     ", received_at="")
        suppressed, rule = apply(msg, config_with_regex_filter)
        assert suppressed
        assert rule is not None
        assert rule.type == "regex"

    def test_regex_no_match(self, config_with_regex_filter):
        config_with_regex_filter.senders["+1555"] = {"name": "X", "allowed": True, "phone": "+1555"}
        msg = Message(id="1", sender="+1555", body="hello world", received_at="")
        suppressed, rule = apply(msg, config_with_regex_filter)
        assert not suppressed

    def test_sender_suppress_via_senders_list(self, config_with_sender_filter):
        """v3: a sender listed with allowed=False is suppressed by the senders list."""
        msg = Message(id="1", sender="+15550001111", body="hello", received_at="")
        suppressed, rule = apply(msg, config_with_sender_filter)
        assert suppressed
        # No FilterRule matched — the synthetic `sender_action` rule
        # marker on entry.rules is a dict, not a FilterRule, so the
        # `rule` return is None.
        assert rule is None

    def test_sender_no_match_via_senders_list(self, config_with_sender_filter):
        """A sender NOT in the senders list (with enforcement on) is suppressed."""
        msg = Message(id="1", sender="+15551234567", body="hello", received_at="")
        suppressed, rule = apply(msg, config_with_sender_filter)
        assert suppressed
        assert rule is None

    def test_sender_allowed_in_senders_list_passes(self, default_config):
        """An allowlisted (allowed=True) sender renders."""
        default_config.senders["+15551234567"] = {
            "name": "Alice",
            "allowed": True,
            "phone": "+15551234567",
        }
        msg = Message(id="1", sender="+15551234567", body="hello", received_at="")
        suppressed, rule = apply(msg, default_config)
        assert not suppressed

    def test_message_uuid_suppress(self, config_with_message_filter):
        config_with_message_filter.senders["+1555"] = {"name": "X", "allowed": True, "phone": "+1555"}
        msg = Message(id="msg-002", sender="+1555", body="hello", received_at="")
        suppressed, rule = apply(msg, config_with_message_filter)
        assert suppressed
        assert rule is not None
        assert rule.type == "message"
        assert rule.pattern == "msg-002"

    def test_first_matching_rule_wins(self):
        """When a real FilterRule matches, it appears in entry.rules[0] (overrides synthetic)."""
        cfg = SignConfig(
            version=3,
            filters=[
                FilterRule(type="keyword", pattern="bad", action="suppress"),
                FilterRule(type="keyword", pattern="badder", action="suppress"),
            ],
            senders={"+15550001111": {"name": "X", "allowed": True, "phone": "+15550001111"}},
        )
        msg = Message(id="1", sender="+15550001111", body="has bad word", received_at="")
        suppressed, rule = apply(msg, cfg)
        assert suppressed
        assert rule is not None
        assert rule.type == "keyword"
        assert rule.pattern == "bad"

    def test_disabled_filter_rule_does_not_suppress(self, default_config):
        """A FilterRule with status='disabled' is skipped (not applied)."""
        default_config.filters.append(
            FilterRule(type="keyword", pattern="badword", action="suppress", status="disabled")
        )
        default_config.senders["+1555"] = {"name": "X", "allowed": True, "phone": "+1555"}
        msg = Message(id="1", sender="+1555", body="has badword in it", received_at="")
        suppressed, rule = apply(msg, default_config)
        assert not suppressed
        assert rule is None

    def test_enforcement_disabled_bypasses_senders_list(self, default_config):
        """When enforce_allowed_senders=False, the senders list is bypassed entirely."""
        # Empty senders dict, enforcement off → everything renders.
        default_config.sign_settings.enforce_allowed_senders = False
        msg = Message(id="1", sender="+15559999999", body="hi", received_at="")
        suppressed, rule = apply(msg, default_config)
        assert not suppressed


class TestGetMessages:
    def test_returns_descending_order(self, sample_messages, default_config):
        # Allowlist every sender in the fixture so senders list doesn't suppress.
        for m in sample_messages:
            default_config.senders[m.sender] = {"name": "X", "allowed": True, "phone": m.sender}
        result = get_messages(sample_messages, default_config)
        ids = [m.id for m in result]
        assert ids == ["msg-003", "msg-002", "msg-001"]

    def test_excludes_suppressed(self, sample_messages, config_with_keyword_filter):
        # Allowlist the two non-suppressed senders.
        for m in [sample_messages[0], sample_messages[2]]:
            config_with_keyword_filter.senders[m.sender] = {
                "name": "X",
                "allowed": True,
                "phone": m.sender,
            }
        result = get_messages(sample_messages, config_with_keyword_filter)
        ids = [m.id for m in result]
        assert "msg-002" not in ids
        assert "msg-001" in ids
        assert "msg-003" in ids

    def test_include_filtered_true_returns_dicts(self, sample_messages, config_with_keyword_filter):
        for m in sample_messages:
            config_with_keyword_filter.senders[m.sender] = {
                "name": "X",
                "allowed": True,
                "phone": m.sender,
            }
        result = get_messages(sample_messages, config_with_keyword_filter, include_filtered=True)
        assert isinstance(result, list)
        assert all(isinstance(r, dict) for r in result)
        suppressed_entries = [r for r in result if r["suppressed"]]
        assert len(suppressed_entries) == 1
        assert suppressed_entries[0]["message"].id == "msg-002"
        assert suppressed_entries[0]["rules"][0]["type"] == "keyword"

    def test_since_filters_by_timestamp(self, sample_messages, default_config):
        for m in sample_messages:
            default_config.senders[m.sender] = {"name": "X", "allowed": True, "phone": m.sender}
        result = get_messages(sample_messages, default_config, since="2026-05-08T11:00:00Z")
        ids = [m.id for m in result]
        assert ids == ["msg-003"]

    def test_since_strictly_after(self, sample_messages, default_config):
        for m in sample_messages:
            default_config.senders[m.sender] = {"name": "X", "allowed": True, "phone": m.sender}

        result = get_messages(sample_messages, default_config, since="2026-05-08T12:00:00Z")
        assert [m.id for m in result] == []

        result2 = get_messages(sample_messages, default_config, since="2026-05-08T11:00:00Z")
        assert [m.id for m in result2] == ["msg-003"]


class TestShouldRenderSender:
    """Direct tests of the v3 should_render_sender helper."""

    def test_unlisted_sender_returns_false_when_enforcement_on(self):
        assert should_render_sender("+15559999999", {}, True) is False

    def test_unlisted_sender_returns_true_when_enforcement_off(self):
        assert should_render_sender("+15559999999", {}, False) is True

    def test_allowed_sender_returns_true(self):
        senders = {"+15559999999": {"name": "A", "allowed": True, "phone": "+15559999999"}}
        assert should_render_sender("+15559999999", senders, True) is True

    def test_disallowed_sender_returns_false(self):
        senders = {"+15559999999": {"name": "A", "allowed": False, "phone": "+15559999999"}}
        assert should_render_sender("+15559999999", senders, True) is False

    def test_disallowed_sender_returns_true_when_enforcement_off(self):
        """Master toggle bypasses per-entry allowed=False."""
        senders = {"+15559999999": {"name": "A", "allowed": False, "phone": "+15559999999"}}
        assert should_render_sender("+15559999999", senders, False) is True

    def test_phone_normalized_on_lookup(self):
        """Lookup matches via the canonical normalize_phone key."""
        senders = {"+15559999999": {"name": "A", "allowed": True, "phone": "+15559999999"}}
        # Variant formats that all normalize to +15559999999
        for fmt in ("+1 (555) 999-9999", "5559999999", "555-999-9999"):
            assert should_render_sender(fmt, senders, True) is True
