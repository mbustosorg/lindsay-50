"""Message filtering for Flask and ESP32.

Filter rule types:
  - keyword : case-insensitive substring match on body
  - regex   : Python `re` full match on body (ESP32 uses `ure`)
  - sender  : exact E.164 phone number match on sender field
  - message : exact UUID match on message id field

Rules are evaluated in order; the first matching rule with action="suppress"
stops the message from being displayed.
"""

import re
from typing import Optional

from .models import Config, FilterRule, Message


def apply(msg: Message, cfg: Config) -> tuple[bool, Optional[FilterRule]]:
    """Apply filter rules to a message.

    Returns (suppressed: bool, matched_rule: FilterRule | None).
    If suppressed is True, matched_rule is the rule that suppressed it.
    If suppressed is False, matched_rule is None.
    """
    for rule in cfg.filters:
        if rule.action != "suppress":
            continue
        if _matches(msg, rule):
            return True, rule
    return False, None


def _matches(msg: Message, rule: FilterRule) -> bool:
    """Return True if the message matches the given filter rule."""
    if rule.type == "keyword":
        return rule.pattern.lower() in msg.body.lower()
    elif rule.type == "regex":
        return bool(re.fullmatch(rule.pattern, msg.body))
    elif rule.type == "sender":
        return msg.sender == rule.pattern
    elif rule.type == "message":
        return msg.id == rule.pattern
    return False


def get_messages(
    messages: list[Message],
    cfg: Config,
    include_filtered: bool = False,
    since: Optional[str] = None,
) -> list[Message] | list[dict]:
    """Return filtered messages ordered by received_at descending.

    Args:
        messages:    All messages to filter.
        cfg:         Current config containing filter rules.
        include_filtered: If True, return suppressed messages with suppression info.
        since:       ISO 8601 timestamp; only return messages received after this time.

    Returns:
        If include_filtered is False: list[Message] of non-suppressed messages.
        If include_filtered is True: list[dict] with keys {message, suppressed, rule}.
    """
    # Filter by timestamp first
    if since:
        filtered = [m for m in messages if m.received_at > since]
    else:
        filtered = list(messages)

    # Ensure descending order (spec requires this regardless of input order)
    filtered.sort(key=lambda m: m.received_at, reverse=True)

    # Apply filter rules in order
    suppressed_map: dict[str, tuple[bool, Optional[FilterRule]]] = {}
    for msg in filtered:
        suppressed, rule = apply(msg, cfg)
        suppressed_map[msg.id] = (suppressed, rule)

    # Build result in descending order
    if not include_filtered:
        return [m for m in filtered if not suppressed_map[m.id][0]]
    else:
        result = []
        for msg in filtered:
            supp, rule = suppressed_map[msg.id]
            entry = {
                "message": msg,
                "suppressed": supp,
            }
            if supp and rule:
                entry["rule"] = rule.to_dict()
            result.append(entry)
        return result


# Alias for admin UI preview page
display_list = get_messages
