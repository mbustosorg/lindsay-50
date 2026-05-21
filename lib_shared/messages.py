"""In-memory message service for ESP32 / CircuitPython.

No ABCs or dataclasses — plain Python only for CircuitPython compatibility.
"""

from collections import deque

from lib_shared.models import FilteredMessages, MessageView


# ---------------------------------------------------------------------------
# In-memory implementation (used by ESP32 / subscriber)
# ---------------------------------------------------------------------------

class InMemoryMessages(FilteredMessages):
    """In-memory ring buffer with O(1) deduplication.

    Uses a deque for the ring buffer and a set for fast seen-id lookup.
    """

    def __init__(self, config, maxlen=100):
        super().__init__(config)
        self._msgs = deque(maxlen=maxlen)
        self._seen_ids = set()

    def add(self, message, source="rest"):
        """Add a single message. Skips if id already seen."""
        if message.id in self._seen_ids:
            return
        self._seen_ids.add(message.id)
        self._msgs.append(
            MessageView(message, source=source, suppressed=False, rules=[], sender_name=None)
        )

    def add_many(self, messages, source="rest"):
        """Add multiple messages in order."""
        for msg in messages:
            self.add(msg, source)

    def clear(self):
        """Clear all messages and seen ids."""
        self._msgs.clear()
        self._seen_ids.clear()

    def get_messages(self, limit=100):
        """Return the most recent N messages, newest first (sorted by received_at desc)."""
        entries = list(self._msgs)
        self._apply_suppression(entries)
        return sorted(entries, key=lambda e: e.message.received_at, reverse=True)[:limit]
