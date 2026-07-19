"""Tests for the filter-rule-status capability (tasks 2.6 + 4.6).

Covers the FilterRule.status field, action=suppress-only enforcement, the
removal of type=sender from the wire, and the _apply_filter skip of disabled
rules.
"""

import pytest

from lib_shared.messages import FilteredMessages
from lib_shared.models import FilterRule, Message, SignConfig

# ---------------------------------------------------------------------------
# Task 2.6 — FilterRule model
# ---------------------------------------------------------------------------


def test_default_status_enabled():
    assert FilterRule(type="keyword", pattern="spam").status == "enabled"


def test_from_dict_missing_status_defaults_enabled():
    rule = FilterRule.from_dict({"type": "keyword", "pattern": "spam"})
    assert rule.status == "enabled"


def test_from_dict_parses_disabled_status():
    rule = FilterRule.from_dict({"type": "keyword", "pattern": "spam", "status": "disabled"})
    assert rule.status == "disabled"


def test_from_dict_rejects_action_allow():
    with pytest.raises(ValueError):
        FilterRule.from_dict({"type": "keyword", "pattern": "spam", "action": "allow"})


def test_from_dict_rejects_type_sender():
    with pytest.raises(ValueError):
        FilterRule.from_dict({"type": "sender", "pattern": "+15551234567"})


@pytest.mark.parametrize("rule_type", ["keyword", "regex", "message"])
def test_from_dict_accepts_valid_types(rule_type):
    rule = FilterRule.from_dict({"type": rule_type, "pattern": "x"})
    assert rule.type == rule_type


def test_to_dict_includes_status():
    rule = FilterRule(type="keyword", pattern="spam", status="disabled")
    assert rule.to_dict() == {
        "type": "keyword",
        "pattern": "spam",
        "action": "suppress",
        "status": "disabled",
    }


# ---------------------------------------------------------------------------
# Task 4.6 — _apply_filter skips disabled rules
# ---------------------------------------------------------------------------


def _msg(body="this is spam"):
    return Message(id="m1", sender="+15551234567", body=body, received_at="2026-06-01T12:00:00Z")


def _fm(filters):
    return FilteredMessages(SignConfig(version=3, filters=filters))


def test_apply_filter_empty_when_all_disabled():
    fm = _fm([FilterRule(type="keyword", pattern="spam", status="disabled")])
    assert fm._apply_filter(_msg(), fm._config.filters) == []


def test_apply_filter_returns_enabled_matching_rule():
    rule = FilterRule(type="keyword", pattern="spam", status="enabled")
    fm = _fm([rule])
    assert fm._apply_filter(_msg(), fm._config.filters) == [rule]


def test_apply_filter_skips_disabled_even_when_pattern_matches():
    disabled = FilterRule(type="keyword", pattern="spam", status="disabled")
    enabled = FilterRule(type="keyword", pattern="ham", status="enabled")
    fm = _fm([disabled, enabled])
    # "spam" matches the disabled rule, "ham" does not match — nothing suppresses.
    assert fm._apply_filter(_msg("this is spam"), fm._config.filters) == []
