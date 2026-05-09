"""Shared pytest fixtures for heart-sms-receiver tests."""

import sys
import tempfile
from pathlib import Path

import pytest

# Ensure lib/ is on the path
sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.models import Config, FilterRule, Message, RenderingSettings, SignSettings

# ---------------------------------------------------------------------------
# Sample data fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_messages():
    return [
        Message(id="msg-001", sender="+15551234567", body="Hello world", received_at="2026-05-08T10:00:00Z"),
        Message(id="msg-002", sender="+15559876543", body="badword inside", received_at="2026-05-08T11:00:00Z"),
        Message(id="msg-003", sender="+15550001111", body="Another message", received_at="2026-05-08T12:00:00Z"),
    ]


@pytest.fixture
def default_config():
    return Config(
        version=1,
        allowed_senders=[],
        filters=[],
        rendering=RenderingSettings(),
        sign=SignSettings(),
    )


@pytest.fixture
def config_with_keyword_filter():
    return Config(
        version=1,
        allowed_senders=[],
        filters=[FilterRule(type="keyword", pattern="badword", action="suppress")],
        rendering=RenderingSettings(),
        sign=SignSettings(),
    )


@pytest.fixture
def config_with_sender_filter():
    return Config(
        version=1,
        allowed_senders=[],
        filters=[FilterRule(type="sender", pattern="+15550001111", action="suppress")],
        rendering=RenderingSettings(),
        sign=SignSettings(),
    )


@pytest.fixture
def config_with_regex_filter():
    return Config(
        version=1,
        allowed_senders=[],
        filters=[FilterRule(type="regex", pattern=r"^\s*$", action="suppress")],
        rendering=RenderingSettings(),
        sign=SignSettings(),
    )


@pytest.fixture
def config_with_message_filter():
    return Config(
        version=1,
        allowed_senders=[],
        filters=[FilterRule(type="message", pattern="msg-002", action="suppress")],
        rendering=RenderingSettings(),
        sign=SignSettings(),
    )


@pytest.fixture
def temp_db(tmp_path):
    """Provide a temporary SQLite database path for storage tests."""
    db = tmp_path / "test.db"
    # Patch _db_path to return the temp path
    import lib.storage as storage_module
    original_path = storage_module._db_path
    storage_module._db_path = lambda: db
    yield db
    storage_module._db_path = original_path
