"""Tests for filter logic in lib_shared/messages.py (FilteredMessages)."""

import pytest
from lib_shared.messages import FilteredMessages, InMemoryMessages
from lib_shared.models import FilterRule, Message, SignConfig

# ---------------------------------------------------------------------------
# Helpers matching the original test API surface
# ---------------------------------------------------------------------------


def apply(msg, cfg):
    """Return (suppressed: bool, first_matching_rule: FilterRule or None)."""
    fm = FilteredMessages(cfg)
    suppressing = fm._apply_filter(msg, cfg.filters)
    if suppressing:
        return True, suppressing[0]
    return False, None


def get_messages(msgs, cfg, include_filtered=False, since=None):
    """Filter msgs by cfg rules, return newest-first list.

    Mirrors the original test API surface using InMemoryMessages.
    """
    store = InMemoryMessages(cfg, maxlen=100)
    for m in msgs:
        store.add(m)
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
        version=1,
        filters=[],
        senders={},
    )


@pytest.fixture
def config_with_keyword_filter():
    return SignConfig(
        version=1,
        filters=[FilterRule(type="keyword", pattern="badword", action="suppress")],
        senders={},
    )


@pytest.fixture
def config_with_sender_filter():
    return SignConfig(
        version=1,
        filters=[FilterRule(type="sender", pattern="+15550001111", action="suppress")],
        senders={},
    )


@pytest.fixture
def config_with_regex_filter():
    return SignConfig(
        version=1,
        filters=[FilterRule(type="regex", pattern=r"^\s*$", action="suppress")],
        senders={},
    )


@pytest.fixture
def config_with_message_filter():
    return SignConfig(
        version=1,
        filters=[FilterRule(type="message", pattern="msg-002", action="suppress")],
        senders={},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestApply:
    def test_no_filters_passes(self, sample_messages, default_config):
        for msg in sample_messages:
            suppressed, rule = apply(msg, default_config)
            assert not suppressed
            assert rule is None

    def test_keyword_suppress_case_insensitive(self, default_config):
        default_config.filters.append(FilterRule(type="keyword", pattern="BADWORD", action="suppress"))
        msg = Message(id="1", sender="+1555", body="has badword in it", received_at="")
        suppressed, rule = apply(msg, default_config)
        assert suppressed
        assert rule.type == "keyword"
        assert rule.pattern == "BADWORD"

    def test_keyword_no_match(self, default_config):
        default_config.filters.append(FilterRule(type="keyword", pattern="badword", action="suppress"))
        msg = Message(id="1", sender="+1555", body="clean message", received_at="")
        suppressed, rule = apply(msg, default_config)
        assert not suppressed

    def test_regex_suppress(self, config_with_regex_filter):
        msg = Message(id="1", sender="+1555", body="     ", received_at="")
        suppressed, rule = apply(msg, config_with_regex_filter)
        assert suppressed
        assert rule.type == "regex"

    def test_regex_no_match(self, config_with_regex_filter):
        msg = Message(id="1", sender="+1555", body="hello world", received_at="")
        suppressed, rule = apply(msg, config_with_regex_filter)
        assert not suppressed

    def test_sender_suppress(self, config_with_sender_filter):
        msg = Message(id="1", sender="+15550001111", body="hello", received_at="")
        suppressed, rule = apply(msg, config_with_sender_filter)
        assert suppressed
        assert rule.type == "sender"
        assert rule.pattern == "+15550001111"

    def test_sender_no_match(self, config_with_sender_filter):
        msg = Message(id="1", sender="+15551234567", body="hello", received_at="")
        suppressed, rule = apply(msg, config_with_sender_filter)
        assert not suppressed

    def test_message_uuid_suppress(self, config_with_message_filter):
        msg = Message(id="msg-002", sender="+1555", body="hello", received_at="")
        suppressed, rule = apply(msg, config_with_message_filter)
        assert suppressed
        assert rule.type == "message"
        assert rule.pattern == "msg-002"

    def test_first_matching_rule_wins(self):
        cfg = SignConfig(
            version=1,
            filters=[
                FilterRule(type="keyword", pattern="bad", action="suppress"),
                FilterRule(type="sender", pattern="+15550001111", action="suppress"),
            ],
            senders={},
        )
        msg = Message(id="1", sender="+15550001111", body="has bad word", received_at="")
        suppressed, rule = apply(msg, cfg)
        assert suppressed
        assert rule.type == "keyword"
        assert rule.pattern == "bad"


class TestGetMessages:
    def test_returns_descending_order(self, sample_messages, default_config):
        result = get_messages(sample_messages, default_config)
        ids = [m.id for m in result]
        assert ids == ["msg-003", "msg-002", "msg-001"]

    def test_excludes_suppressed(self, sample_messages, config_with_keyword_filter):
        result = get_messages(sample_messages, config_with_keyword_filter)
        ids = [m.id for m in result]
        assert "msg-002" not in ids
        assert "msg-001" in ids
        assert "msg-003" in ids

    def test_include_filtered_true_returns_dicts(self, sample_messages, config_with_keyword_filter):
        result = get_messages(sample_messages, config_with_keyword_filter, include_filtered=True)
        assert isinstance(result, list)
        assert all(isinstance(r, dict) for r in result)
        suppressed_entries = [r for r in result if r["suppressed"]]
        assert len(suppressed_entries) == 1
        assert suppressed_entries[0]["message"].id == "msg-002"
        assert suppressed_entries[0]["rules"][0]["type"] == "keyword"

    def test_since_filters_by_timestamp(self, sample_messages, default_config):
        result = get_messages(sample_messages, default_config, since="2026-05-08T11:00:00Z")
        ids = [m.id for m in result]
        assert ids == ["msg-003"]

    def test_since_strictly_after(self, sample_messages, default_config):
        result = get_messages(sample_messages, default_config, since="2026-05-08T12:00:00Z")
        assert [m.id for m in result] == []

        result2 = get_messages(sample_messages, default_config, since="2026-05-08T11:00:00Z")
        assert [m.id for m in result2] == ["msg-003"]
