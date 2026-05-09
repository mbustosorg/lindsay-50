"""Tests for lib/filters.py."""

import pytest
from lib.filters import apply, get_messages
from lib.models import Config, FilterRule, Message, RenderingSettings, SignSettings


class TestApply:
    def test_no_filters_passes(self, sample_messages, default_config):
        for msg in sample_messages:
            suppressed, rule = apply(msg, default_config)
            assert not suppressed
            assert rule is None

    def test_keyword_suppress_case_insensitive(self, default_config):
        cfg = Config(
            version=1, allowed_senders=[], filters=[],
            rendering=RenderingSettings(), sign=SignSettings(),
        )
        cfg.filters.append(FilterRule(type="keyword", pattern="BADWORD", action="suppress"))
        msg = Message(id="1", sender="+1555", body="has badword in it", received_at="")

        suppressed, rule = apply(msg, cfg)
        assert suppressed
        assert rule.type == "keyword"
        assert rule.pattern == "BADWORD"

    def test_keyword_no_match(self, default_config):
        cfg = Config(
            version=1, allowed_senders=[], filters=[],
            rendering=RenderingSettings(), sign=SignSettings(),
        )
        cfg.filters.append(FilterRule(type="keyword", pattern="badword", action="suppress"))
        msg = Message(id="1", sender="+1555", body="clean message", received_at="")

        suppressed, rule = apply(msg, cfg)
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
        """When multiple rules match, the first one in the list suppresses."""
        cfg = Config(
            version=1, allowed_senders=[],
            filters=[
                FilterRule(type="keyword", pattern="bad", action="suppress"),
                FilterRule(type="sender", pattern="+15550001111", action="suppress"),
            ],
            rendering=RenderingSettings(), sign=SignSettings(),
        )
        msg = Message(id="1", sender="+15550001111", body="has bad word", received_at="")
        suppressed, rule = apply(msg, cfg)
        assert suppressed
        assert rule.type == "keyword"  # first rule wins
        assert rule.pattern == "bad"


class TestGetMessages:
    def test_returns_descending_order(self, sample_messages, default_config):
        # sample_messages fixture: msg-001=T01, msg-002=T02, msg-003=T03
        # Descending by received_at: msg-003 (T03), msg-002 (T02), msg-001 (T01)
        result = get_messages(sample_messages, default_config)
        ids = [m.id for m in result]
        assert ids == ["msg-003", "msg-002", "msg-001"]

    def test_excludes_suppressed(self, sample_messages, config_with_keyword_filter):
        result = get_messages(sample_messages, config_with_keyword_filter)
        ids = [m.id for m in result]
        # msg-002 body is "badword inside" which contains "badword" → suppressed → NOT in result
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
        assert suppressed_entries[0]["rule"]["type"] == "keyword"

    def test_since_filters_by_timestamp(self, sample_messages, default_config):
        # sample_messages: msg-001=T10, msg-002=T11, msg-003=T12 (descending: msg-003, msg-002, msg-001)
        # Query for > T11: only msg-003 (T12) qualifies
        result = get_messages(sample_messages, default_config, since="2026-05-08T11:00:00Z")
        ids = [m.id for m in result]
        assert ids == ["msg-003"]

    def test_since_strictly_after(self, sample_messages, default_config):
        """Messages with received_at exactly equal to 'since' are excluded."""
        # sample_messages: msg-001=T10, msg-002=T11, msg-003=T12 (descending: msg-003, msg-002, msg-001)
        # Query for > T12: nothing is strictly after T12
        result = get_messages(sample_messages, default_config, since="2026-05-08T12:00:00Z")
        assert [m.id for m in result] == []

        # Query for > T11: only msg-003 (T12) qualifies
        result2 = get_messages(sample_messages, default_config, since="2026-05-08T11:00:00Z")
        assert [m.id for m in result2] == ["msg-003"]
