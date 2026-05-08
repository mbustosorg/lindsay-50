"""Filter engine using Python `re`. Identical logic to ESP32 `ure` version."""

import re

from lib.models import Config, Message


def apply(message: Message, config: Config) -> bool:
    """
    Return True if the message should be suppressed, False otherwise.

    Rules are evaluated in order; first matching suppress rule wins.
    """
    body = message.body
    sender = message.sender
    msg_id = message.id

    for rule in config.filters:
        if rule.action != "suppress":
            continue
        if rule.type == "keyword":
            if rule.pattern.lower() in body.lower():
                return True
        elif rule.type == "regex":
            if re.search(rule.pattern, body) is not None:
                return True
        elif rule.type == "sender":
            if rule.pattern == sender:
                return True
        elif rule.type == "message":
            if rule.pattern == msg_id:
                return True
    return False


def display_list(messages: list[Message], config: Config) -> list[Message]:
    """
    Return only the non-suppressed messages, sorted by received_at ascending.
    """
    suppressed = {msg.id for msg in messages if apply(msg, config)}
    return [msg for msg in sorted(messages, key=lambda m: m.received_at) if msg.id not in suppressed]
