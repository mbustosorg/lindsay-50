"""SQLite-backed config for Flask.

SqliteConfig inherits from Config and persists to SQLite on update.
"""

from lib import storage
from lib_shared.models import Config


class SqliteConfig(Config):
    """Config that persists to SQLite on update()."""

    def __init__(self):
        super().__init__()

    def update(self, other: Config) -> None:
        """Update from another Config and persist to SQLite."""
        super().update(other)
        # Persist the whole config to SQLite after the update
        storage.put_config(self)