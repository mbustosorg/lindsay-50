"""SQLite-backed message service for Flask.

SqliteMessages wraps storage to implement FilteredMessages.
"""

from lib import storage
from lib_shared.models import FilteredMessages, Message, MessageView


class SqliteMessages(FilteredMessages):
    """Message service backed by SQLite.

    Source is always 'rest' for SQLite-backed messages.
    """

    def add(self, message: Message, source: str = "rest") -> None:
        """Store a message to SQLite."""
        storage.put_message(message)

    def add_many(self, messages, source: str = "rest") -> None:
        """Store multiple messages to SQLite."""
        for msg in messages:
            storage.put_message(msg)

    def clear(self) -> None:
        """Clear all messages from SQLite (not supported for safety)."""
        raise NotImplementedError("Clearing all messages from SQLite is not supported")

    def get_messages(self, limit: int = 100):
        """Return messages from SQLite with suppression applied, newest first."""
        all_msgs = storage.get_all_messages()
        page_msgs = all_msgs[-limit:]

        entries = []
        for msg in reversed(page_msgs):
            entries.append(MessageView(
                message=msg,
                source="rest",
                suppressed=False,
                rules=[],
                sender_name=None,
            ))

        self._apply_suppression(entries)
        return entries
