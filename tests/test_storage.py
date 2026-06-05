"""Tests for heart-message-manager/sqlite.py."""

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Mock lib_shared.models before importing sqlite
lib_shared_models = importlib.import_module("lib_shared.models")
sys.modules["lib_shared"] = MagicMock()
sys.modules["lib_shared.models"] = lib_shared_models

# Import sqlite via importlib (hyphenated package name)
_sqlite = importlib.import_module("heart-message-manager.sqlite")
storage = _sqlite

from lib_shared.models import (
    FilterRule,
    Message,
    RenderingSettings,
    SignConfig,
    SignSettings,
)


@pytest.fixture
def temp_db(tmp_path):
    """Provide a temporary SQLite database path for storage tests."""
    db = tmp_path / "test.db"
    with patch.object(storage, "_db_path", lambda: db):
        yield db
