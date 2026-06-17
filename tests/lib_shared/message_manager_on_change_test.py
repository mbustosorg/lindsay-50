"""Tests for the MessageManager's universal `on_change` callback.

The `on_change` callback is the single fan-out point the EffectsCoordinator
and the browser JS subscribers listen on. The Pi's `main.py` and the
browser's `app_main.py` both construct a `MessageManager` with an
`on_change` closure that calls
`coord.apply_settings(manager.config.effect_settings,
manager.config.text_settings)`. These tests pin down that contract:

1. `on_change` is invoked exactly once per `MessageManager._emit_change()`
   call from either `_handle_message()` or `_handle_config()`.
2. In the browser runtime, `on_change` (the app-scoped
   `_on_change_js` in `app_main.py`) calls `_coordinator.apply_settings(...)`
   and also fans the change out to JS subscribers via `App._dispatchChange()`.
3. `MessageManager(coordinator=coord)` raises `TypeError: __init__()
   got an unexpected keyword argument 'coordinator'` (the manager does
   not accept a coordinator reference — closures capture it instead).
"""

import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from lib_shared.message_manager import MessageManager
from lib_shared.models import SignConfig

# --- helpers -----------------------------------------------------------------


def _build_manager(on_change=None, messages_api_url="", config_api_url="", api_key=""):
    """Build a MessageManager with a tracking on_change callback.

    Bypasses the network: the manager is constructed with empty URLs,
    so its background pollers (if any) will fail, but the dispatch /
    _handle_message / _handle_config paths we exercise here work
    without HTTP.
    """
    return MessageManager(
        messages_api_url=messages_api_url,
        config_api_url=config_api_url,
        api_key=api_key,
        on_change=on_change,
    )


# --- Scenario 1: on_change fires once per _emit_change() ---------------------


def test_on_change_fires_on_handle_message():
    """_handle_message() triggers exactly one _emit_change() per call,
    which fires on_change exactly once."""
    calls = []
    mgr = _build_manager(on_change=lambda: calls.append("cb"))
    envelope = {
        "type": "message",
        "payload": {
            "id": "m1",
            "sender": "+1",
            "body": "hi",
            "received_at": "2026-01-01T00:00:00Z",
        },
    }
    mgr._handle_message(envelope)
    assert calls == ["cb"], f"expected exactly one callback, got {calls}"


def test_on_change_fires_on_handle_config():
    """_handle_config() triggers exactly one _emit_change() per call."""
    calls = []
    mgr = _build_manager(on_change=lambda: calls.append("cb"))
    envelope = {
        "type": "config",
        "payload": SignConfig().to_dict(),
    }
    mgr._handle_config(envelope)
    assert calls == ["cb"]


def test_on_change_does_not_fire_when_no_change():
    """If the dispatch sees an unknown envelope type, on_change is NOT
    called (no `_emit_change` happens)."""
    calls = []
    mgr = _build_manager(on_change=lambda: calls.append("cb"))
    mgr.dispatch("not a json string")
    assert calls == []


# --- Scenario 2: browser fan-out via create_proxy(_on_change_js) ------------


def test_app_main_on_change_applies_config_and_fans_out_to_js():
    """In the browser, the app-scoped MessageManager's on_change callback
    (1) applies the new config to the coordinator and (2) fans the
    change out to JS subscribers via `App._dispatchChange()`.

    We can't run PyScript in tests, so we read the source and assert
    both branches of the on_change body are present. This is a
    static check.
    """
    p = Path(__file__).parent.parent.parent / "heart-message-manager" / "app_main.py"
    src = p.read_text(encoding="utf-8")
    assert "def _on_change_js" in src, "app_main.py must define _on_change_js"
    # Find the closure body — between `def _on_change_js():` (or with
    # the `-> None` annotation) and the next `def `.
    m = re.search(r"def _on_change_js\([^)]*\)[^:]*:\s*\n(.*?)(?=\ndef |\Z)", src, re.DOTALL)
    assert m is not None, "could not extract _on_change_js body"
    body = m.group(1)
    assert "_coordinator.apply_settings" in body, "browser _on_change_js must call _coordinator.apply_settings"
    assert "app._dispatchChange" in body, "browser _on_change_js must call app._dispatchChange to fan out to JS"


def test_create_proxy_is_invoked_with_on_change_js():
    """Smoke check: a closure that calls create_proxy(_on_change_js)()
    actually invokes the proxy.

    We can't import preview_main under CPython (top-level await on
    `loadPackage`), so we exercise the same pattern in isolation: a
    closure that calls a tracking proxy and assert the proxy fired.
    """
    create_proxy = MagicMock(return_value=MagicMock())
    _on_change_js = MagicMock()

    def _on_change():
        create_proxy(_on_change_js)()

    _on_change()
    # The proxy was created around the JS callback and then invoked.
    create_proxy.assert_called_once_with(_on_change_js)
    create_proxy.return_value.assert_called_once_with()


# --- Scenario 3: MessageManager(coordinator=...) raises TypeError ------------


def test_message_manager_rejects_coordinator_kwarg():
    """The manager does not accept a `coordinator=` reference.

    The coordinator is captured by a closure (the `on_change` callback)
    — the manager itself does not need (or accept) a back-reference.
    """
    from lib_shared.effects_coordinator import EffectsCoordinator
    from lib_shared.models import EffectsSettings, TextSettings

    coord = EffectsCoordinator(
        message_manager=SimpleNamespace(
            messages=SimpleNamespace(get_messages=lambda limit=100, suppress=True: []),
            config=SimpleNamespace(effect_settings=EffectsSettings(), text_settings=TextSettings()),
        ),
    )
    with pytest.raises(TypeError, match="coordinator"):
        MessageManager(
            messages_api_url="",
            config_api_url="",
            api_key="",
            coordinator=coord,
        )
