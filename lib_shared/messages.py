"""In-memory message service for ESP32 / CircuitPython.

No ABCs or dataclasses — plain Python only for CircuitPython compatibility.

Classes:
    FilteredMessages: Abstract base; applies filter rules and sender-name resolution.
    InMemoryMessages: Ring-buffer implementation with O(1) deduplication.
"""

import calendar
import re
import time
from collections import deque

from lib_shared.models import MessageView


def _format_display_time(received_at: str, tz_offset_mins: int) -> str:
    """Format a UTC ISO timestamp for display in the sign's configured timezone.

    Uses only ``time`` and ``calendar`` — no ``zoneinfo`` or ``datetime`` — for
    broad CircuitPython compatibility.

    ``time.gmtime(local_epoch)`` is used instead of ``time.localtime()`` to avoid
    the system timezone polluting the result.  We treat the UTC epoch as UTC and
    only apply the configured sign offset.

    Args:
        received_at:    UTC ISO 8601 timestamp, e.g. ``"2026-05-22T14:30:00Z"``.
        tz_offset_mins: Signed UTC offset in minutes for the sign's timezone,
                        e.g. ``-240`` for EDT, ``-420`` for PDT.

    Returns:
        Formatted string, e.g. ``"May 22 10:30 AM"``.
    """
    try:
        time_tuple = time.strptime(received_at, "%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return received_at
    try:
        utc_epoch = calendar.timegm(time_tuple)
    except Exception:
        return received_at
    local_epoch = utc_epoch + tz_offset_mins * 60
    try:
        # time.gmtime always interprets as UTC, so we get the correct UTC
        # components of the local time without the system TZ interfering.
        return time.strftime("%b %d %I:%M %p", time.gmtime(local_epoch))
    except Exception:
        return received_at


class FilteredMessages:
    """Abstract in-memory message store with filter-rule support.

    Subclasses must implement:
      - add(message, source="rest")
      - add_many(messages, source="rest")
      - clear()
      - get_messages(limit=100) -> list[MessageView]

    The base class provides _apply_filter(), _matches(), and _enrich_messages()
    for use by subclasses. Thread-safety is the caller's responsibility.
    """

    def __init__(self, config):
        """Initialize with a SignConfig (provides .filters and .senders)."""
        self._config = config

    def _apply_filter(self, msg, rules):
        """Apply filter rules to a message.

        Args:
            msg:   Message object
            rules: list of FilterRule objects

        Returns:
            List of FilterRule objects that suppress the message (in evaluation order).
        """
        suppressing = []
        for rule in rules:
            if rule.action != "suppress":
                continue
            if self._matches(msg, rule):
                suppressing.append(rule)
        return suppressing

    def _matches(self, msg, rule):
        """Return True if message matches the filter rule."""
        if rule.type == "keyword":
            return rule.pattern.lower() in msg.body.lower()
        elif rule.type == "regex":
            try:
                return bool(re.fullmatch(rule.pattern, msg.body))
            except re.error:
                return False
        elif rule.type == "sender":
            return msg.sender == rule.pattern
        elif rule.type == "message":
            return msg.id == rule.pattern
        return False

    def _enrich_messages(self, entries):
        """Enrich each MessageView entry with suppressed, rules, sender_name, and display_time.

        Called by get_messages() before returning. Override to customize.
        """
        tz_offset = self._config.tz_offset_mins
        for entry in entries:
            suppressing = self._apply_filter(entry.message, self._config.filters)
            entry.suppressed = bool(suppressing)
            entry.rules = [r.to_dict() for r in suppressing]
            entry.sender_name = self._config.senders.get(entry.message.sender)
            entry.display_time = _format_display_time(
                entry.message.received_at, tz_offset
            )

    def add(self, message, source="rest"):
        """Add a single message to the store."""
        raise NotImplementedError()

    def add_many(self, messages, source="rest"):
        """Add multiple messages in order."""
        raise NotImplementedError()

    def clear(self):
        """Clear all messages from the store."""
        raise NotImplementedError()

    def get_messages(self, limit=100, suppress=True):
        """Return MessageView entries, newest first.

        Args:
            limit: Maximum number of messages to return.
            suppress: If True (default), excluded suppressed messages from the result.
        """
        raise NotImplementedError()


class InMemoryMessages(FilteredMessages):
    """In-memory ring buffer with O(1) deduplication.

    Uses a deque for the ring buffer and a set for fast seen-id lookup.
    Duplicates are dropped silently on add().
    """

    def __init__(self, config, maxlen=100):
        """Initialize with a config and optional ring-buffer max length.

        Args:
            config: SignConfig instance providing .filters and .senders.
            maxlen: Maximum number of messages to retain (default 100).
        """
        super().__init__(config)
        self._msgs = deque(maxlen=maxlen)
        self._seen_ids = set()

    def add(self, message, source="rest"):
        """Add a single message. Skips silently if id already seen (O(1) check)."""
        if message.id in self._seen_ids:
            return
        self._seen_ids.add(message.id)
        self._msgs.append(MessageView(message, source=source))

    def add_many(self, messages, source="rest"):
        """Add multiple messages in insertion order. Skips duplicates."""
        for msg in messages:
            self.add(msg, source)

    def clear(self):
        """Clear all messages and the seen-id set."""
        self._msgs.clear()
        self._seen_ids.clear()

    def get_messages(self, limit=100, suppress=True):
        """Return the most recent N messages, newest first (sorted by received_at desc).

        Args:
            limit: Maximum number of messages to return (default 100).
            suppress: If True (default), exclude suppressed messages from the result.
        """
        entries = list(self._msgs)
        self._enrich_messages(entries)
        if suppress:
            entries = [e for e in entries if not e.suppressed]
        return sorted(entries, key=lambda e: e.message.received_at, reverse=True)[
            :limit
        ]
