"""Tests for heart-message-manager/sqlite.py."""

import importlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Load lib_shared.models explicitly before importing sqlite, so the
# real module is in sys.modules regardless of import order. We do
# NOT replace `sys.modules["lib_shared"]` with a MagicMock — that
# leaks into subsequent test files (e.g. effects_coordinator_test
# sees a MagicMock for `lib_shared.effects_coordinator` and
# `_pick_message_via_selector` becomes a Mock instead of the real
# method). sqlite.py only imports from `lib_shared.models`, so the
# package-level MagicMock was unnecessary.
lib_shared_models = importlib.import_module("lib_shared.models")
sys.modules["lib_shared.models"] = lib_shared_models

# Import sqlite via importlib (hyphenated package name)
_sqlite = importlib.import_module("heart-message-manager.sqlite")
storage = _sqlite


@pytest.fixture
def temp_db(tmp_path):
    """Provide a temporary SQLite database path for storage tests."""
    db = tmp_path / "test.db"
    with patch.object(storage, "_db_path", lambda: db):
        yield db
