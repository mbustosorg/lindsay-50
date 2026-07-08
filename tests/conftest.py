"""Shared pytest fixtures for heart-message-manager tests."""

import importlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest

# Ensure project root is on the path so lib_shared is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

# The Pi-side modules (`check_for_update`, `status`) live in
# `heart-matrix-controller/` and aren't a real package (no __init__.py
# that exposes them). Add the directory to sys.path so tests can do
# `from check_for_update import …` / `from status import …` directly.
_HMC_DIR = Path(__file__).parent.parent / "heart-matrix-controller"
sys.path.insert(0, str(_HMC_DIR))

# Expose the hyphenated heart-message-manager/ directory under the
# package name `heart_message_manager` so tests can do
# `from heart_message_manager.preview_scroller import PreviewScroller`.
# Python refuses to import a hyphenated directory as a package
# (PEP 328 / Python's identifier rules), so we synthesize a package
# whose __path__ points at the real directory. preview_scroller and
# preview_display are pure-CPython; preview_main is PyScript-only
# (top-level `await`) and is left for the test that exercises it
# (preview_wiring_test imports it lazily and skips on host CPython).
_HEART_MM_DIR = Path(__file__).parent.parent / "heart-message-manager"
if "heart_message_manager" not in sys.modules:
    _pkg = types.ModuleType("heart_message_manager")
    _pkg.__path__ = [str(_HEART_MM_DIR)]
    sys.modules["heart_message_manager"] = _pkg
    for _mod_name in ("preview_scroller", "preview_display"):
        _path = _HEART_MM_DIR / f"{_mod_name}.py"
        if not _path.exists():
            continue
        _spec = importlib.util.spec_from_file_location(f"heart_message_manager.{_mod_name}", str(_path))
        if _spec is None or _spec.loader is None:
            continue
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules[f"heart_message_manager.{_mod_name}"] = _mod
        _spec.loader.exec_module(_mod)

from lib_shared.models import (
    EffectsSettings,
    FilterRule,
    Message,
    SignConfig,
    SignSettings,
    TextSettings,
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


@pytest.fixture(autouse=True)
def _restore_time_monotonic():
    """Some tests in this repo (e.g. `effects_coordinator_test.py`,
    `lib_shared/effects_coordinator_get_display_message_test.py`) install
    `pytest.MonkeyPatch()` manually and rely on `monkey.undo()` at the
    end of the test. If such a test errors out before `undo()`, the
    patch leaks into later tests and `time.monotonic()` returns a
    frozen value forever — which makes any subsequent loop using it
    hang. We restore the real `time.monotonic` before every test to
    bound the damage. Real `time` doesn't expose `monotonic` as a
    settable attribute in a way we'd break; if a test legitimately
    patches it via the `monkeypatch` fixture, that test's restoration
    runs after this one (monkeypatch undos happen at the end of the
    test) so the test still sees its own mock during execution.
    """
    import time as _time

    real_monotonic = _time.monotonic
    yield
    if _time.monotonic is not real_monotonic:
        _time.monotonic = real_monotonic


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
        version=2,
        filters=[],
        senders={},
        effects_settings=EffectsSettings(),
        text_settings=TextSettings(),
        sign=SignSettings(),
    )


@pytest.fixture
def config_with_keyword_filter():
    return SignConfig(
        version=2,
        filters=[FilterRule(type="keyword", pattern="badword", action="suppress")],
        senders={},
        effects_settings=EffectsSettings(),
        text_settings=TextSettings(),
        sign=SignSettings(),
    )


@pytest.fixture
def config_with_sender_filter():
    return SignConfig(
        version=2,
        filters=[FilterRule(type="sender", pattern="+15550001111", action="suppress")],
        senders={},
        effects_settings=EffectsSettings(),
        text_settings=TextSettings(),
        sign=SignSettings(),
    )


@pytest.fixture
def config_with_regex_filter():
    return SignConfig(
        version=2,
        filters=[FilterRule(type="regex", pattern=r"^\s*$", action="suppress")],
        senders={},
        effects_settings=EffectsSettings(),
        text_settings=TextSettings(),
        sign=SignSettings(),
    )


@pytest.fixture
def config_with_message_filter():
    return SignConfig(
        version=2,
        filters=[FilterRule(type="message", pattern="msg-002", action="suppress")],
        senders={},
        effects_settings=EffectsSettings(),
        text_settings=TextSettings(),
        sign=SignSettings(),
    )
