"""Shared pytest fixtures for heart-message-manager tests."""

import importlib
import sys
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


def _ensure_real_lib_shared():
    """Re-import the real lib_shared package if it was replaced by a Mock.

    `test_auth.py`'s `app` fixture swaps `lib_shared` (and its submodules)
    for `types.ModuleType` mocks so it can stub out heavy deps while
    loading main.py. After that fixture tears down, the real submodules
    are restored — but if a sibling test's autouse fixture runs an
    unconditional wipe-and-reimport (the previous approach), it drops
    the real submodules and forces a fresh import, breaking tests that
    captured references at module load time.

    This helper only wipes when the cached `lib_shared` is a Mock (i.e.
    the package itself has no `__path__`, which a real package always
    has). On a clean process the real package is already in sys.modules
    and the function is a no-op.
    """
    cached = sys.modules.get("lib_shared")
    if cached is None or not hasattr(cached, "__path__"):
        for name in [k for k in list(sys.modules) if k == "lib_shared" or k.startswith("lib_shared.")]:
            del sys.modules[name]
        importlib.import_module("lib_shared")


@pytest.fixture(autouse=True)
def _restore_lib_shared():
    """Per-test guard: re-import the real `lib_shared` package if a Mock
    was left in sys.modules. No-op when the package is already real.
    """
    _ensure_real_lib_shared()
    yield


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
