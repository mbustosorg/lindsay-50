"""Shared pytest fixtures for heart-message-manager tests."""

import sys
import tempfile
from pathlib import Path

import pytest

# Ensure project root is on the path so lib_shared is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from lib_shared.models import (
    FilterRule,
    Message,
    RenderingSettings,
    SignConfig,
    SignSettings,
)

# ---------------------------------------------------------------------------
# Sample data fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_messages():
    return [
        Message(
            id="msg-001",
            sender="+15551234567",
            body="Hello world",
            received_at="2026-05-08T10:00:00Z",
        ),
        Message(
            id="msg-002",
            sender="+15559876543",
            body="badword inside",
            received_at="2026-05-08T11:00:00Z",
        ),
        Message(
            id="msg-003",
            sender="+15550001111",
            body="Another message",
            received_at="2026-05-08T12:00:00Z",
        ),
    ]


@pytest.fixture
def default_config():
    return SignConfig(
        version=1,
        filters=[],
        senders={},
        rendering=RenderingSettings(),
        sign=SignSettings(),
    )


@pytest.fixture
def config_with_keyword_filter():
    return SignConfig(
        version=1,
        filters=[FilterRule(type="keyword", pattern="badword", action="suppress")],
        senders={},
        rendering=RenderingSettings(),
        sign=SignSettings(),
    )


@pytest.fixture
def config_with_sender_filter():
    return SignConfig(
        version=1,
        filters=[FilterRule(type="sender", pattern="+15550001111", action="suppress")],
        senders={},
        rendering=RenderingSettings(),
        sign=SignSettings(),
    )


@pytest.fixture
def config_with_regex_filter():
    return SignConfig(
        version=1,
        filters=[FilterRule(type="regex", pattern=r"^\s*$", action="suppress")],
        senders={},
        rendering=RenderingSettings(),
        sign=SignSettings(),
    )


@pytest.fixture
def config_with_message_filter():
    return SignConfig(
        version=1,
        filters=[FilterRule(type="message", pattern="msg-002", action="suppress")],
        senders={},
        rendering=RenderingSettings(),
        sign=SignSettings(),
    )
