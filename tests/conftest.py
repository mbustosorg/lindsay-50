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

    `test_auth.py`'s `app` fixture (and similar mocks in
    `test_admin_settings_route.py`, `settings_template_test.py`, etc.)
    swap `lib_shared` (and its submodules) for `types.ModuleType` mocks
    so they can stub out heavy deps while loading main.py. These mocks
    set `__path__` to the real directory so Python's import system can
    resolve any submodules they DON'T mock — which means a simple
    `hasattr(cached, '__path__')` check is no longer enough to tell a
    Mock from the real package.

    The reliable signal is `__file__`: a `types.ModuleType` instance
    has no `__file__` attribute unless explicitly set, but the real
    package always has one (it points at `lib_shared/__init__.py`).
    Same for any submodules: `lib_shared.models` Mock objects in
    `test_auth.py` / `test_sign_settings_endpoint.py` don't set
    `__file__`; the real module does.

    The wipe runs only when at least one cached lib_shared* entry is a
    Mock — on a clean process every entry has `__file__` and the
    helper is a no-op.
    """
    cached_names = [k for k in list(sys.modules) if k == "lib_shared" or k.startswith("lib_shared.")]
    if not cached_names:
        return
    has_mock = False
    for name in cached_names:
        mod = sys.modules.get(name)
        if mod is None:
            continue
        if not hasattr(mod, "__file__"):
            has_mock = True
            break
    if not has_mock:
        return
    for name in cached_names:
        sys.modules.pop(name, None)
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


@pytest.fixture(autouse=True)
def _reset_effects_settings_cache():
    """Clear the loader cache (and the per-function cache in
    `_default_effects_list`) after every test that touches it.

    Tests that drive `EFFECTS_SETTINGS_OVERRIDE` (e.g. the override-
    added / deleted-canonical tests in `test_admin_settings_route.py`)
    populate the loader cache with their override's data, then
    `monkeypatch.undo()` removes the env var. The cache, however,
    persists. A sibling test that constructs `EffectsSettings()` (or
    calls `make_effect_class(...)`) without resetting the cache picks
    up the override's pacing values and a stale effects list — the
    `apply_settings` failures on 2026-07-09 trace back to this
    interaction.

    The reset happens AFTER the test (not before) so a test that
    legitimately wants a populated cache can keep it for the duration
    of its own body; the reset only reaches across test boundaries.
    """
    yield
    # The `from lib_shared.models import _default_effects_list` below
    # requires the real models module to be in sys.modules — but tests
    # like `test_auth.py`, `test_flask_command_endpoints.py`, and
    # `messages_archive_test.py` swap it for a Mock-without-`_default_
    # effects_list` while loading main.py. If a sibling test's teardown
    # ran ahead of this autouse fixture's undo (or monkeypatch's undo
    # left a stale entry), the import errors with "cannot import name
    # '_default_effects_list'". Wipe stale Mocks first so the import
    # resolves against the genuine module.
    _ensure_real_lib_shared()
    import lib_shared.effects_loader as _loader
    from lib_shared.models import _default_effects_list

    _loader.reset_effects_settings()
    if hasattr(_default_effects_list, "_cache"):
        delattr(_default_effects_list, "_cache")


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
        version=3,
        filters=[],
        senders={},
        effects_settings=EffectsSettings(),
        text_settings=TextSettings(),
        sign_settings=SignSettings(),
    )


@pytest.fixture
def config_with_keyword_filter():
    return SignConfig(
        version=3,
        filters=[FilterRule(type="keyword", pattern="badword", action="suppress")],
        senders={},
        effects_settings=EffectsSettings(),
        text_settings=TextSettings(),
        sign_settings=SignSettings(),
    )


@pytest.fixture
def config_with_sender_filter():
    # v3: sender-type FilterRule is REMOVED — sender matching now lives in
    # the senders list. Build a config with a `+15550001111` senders entry
    # that has `allowed=False` so the apply path suppresses this sender.
    return SignConfig(
        version=3,
        filters=[],
        senders={"+15550001111": {"name": "Block", "allowed": False, "phone": "+15550001111"}},
        effects_settings=EffectsSettings(),
        text_settings=TextSettings(),
        sign_settings=SignSettings(),
    )


@pytest.fixture
def config_with_regex_filter():
    return SignConfig(
        version=3,
        filters=[FilterRule(type="regex", pattern=r"^\s*$", action="suppress")],
        senders={},
        effects_settings=EffectsSettings(),
        text_settings=TextSettings(),
        sign_settings=SignSettings(),
    )


@pytest.fixture
def config_with_message_filter():
    return SignConfig(
        version=3,
        filters=[FilterRule(type="message", pattern="msg-002", action="suppress")],
        senders={},
        effects_settings=EffectsSettings(),
        text_settings=TextSettings(),
        sign_settings=SignSettings(),
    )
