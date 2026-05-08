import re
from .models import Message, Config


def apply(message: Message, config: Config) -> bool:
    """
    Apply filter rules to a message.
    Returns True if the message should be suppressed, False otherwise.
    """
    for rule in config.filters:
        if rule.action != "suppress":
            continue
        if rule.type == "keyword":
            if rule.pattern.lower() in message.body.lower():
                return True
        elif rule.type == "regex":
            try:
                if re.search(rule.pattern, message.body, re.IGNORECASE):
                    return True
            except re.error:
                pass
        elif rule.type == "sender":
            if message.sender == rule.pattern:
                return True
        elif rule.type == "message":
            if message.id == rule.pattern:
                return True
    return False


def display_list(messages: list[Message], config: Config) -> list[Message]:
    """
    Return only non-suppressed messages, ordered by received_at ascending.
    """
    suppressed = [msg for msg in messages if apply(msg, config)]
    suppressed_ids = {msg.id for msg in suppressed}
    return [msg for msg in sorted(messages, key=lambda m: m.received_at) if msg.id not in suppressed_ids]
