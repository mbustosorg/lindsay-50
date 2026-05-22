"""SQLite-backed config for Flask.

SqliteConfig inherits from SignConfig and persists to SQLite on update.
"""

from lib import storage
from lib_shared.models import SignConfig


class SqliteConfig(SignConfig):
    """Config that persists to SQLite on update()."""

    def __init__(self):
        super().__init__()

    def update(self, other: SignConfig) -> None:
        """Update from another SignConfig and persist to SQLite."""
        super().update(other)
        # Persist the whole config to SQLite after the update
        storage.put_config(self)