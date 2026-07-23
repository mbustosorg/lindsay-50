"""Tests for the MessageManager's universal `on_change` callback.

The `on_change` callback is the single fan-out point the browser JS
subscribers listen on. The Pi's `main.py` and the browser's
`app_main.py` both construct a `MessageManager` with an `on_change`
closure; the closure is a no-op for the Pi (the coordinator reads
config live from the manager) and a fan-out to `App._dispatchChange`
for the browser. The coordinator's config is read live at tick
time — there is no `apply_settings` call from the on_change path.

These tests pin down that contract:

1. `on_change` is invoked exactly once per `MessageManager._emit_change()`
   call from either `_handle_message()` or `_handle_config()`.
2. In the browser runtime, `on_change` (the app-scoped
   `_on_change_js` in `app_main.py`) fans the change out to JS
   subscribers via `App._dispatchChange()`. It does NOT call
   `apply_settings` — the coordinator pulls config live.
3. `MessageManager(coordinator=coord)` raises `TypeError: __init__()
   got an unexpected keyword argument 'coordinator'` (the manager does
   not accept a coordinator reference — the coordinator is constructed
   with `message_manager=...` instead).
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
    # _handle_message takes the inner payload dict (what the
    # dispatcher at message_manager.py:439 extracts from
    # `envelope.payload`), NOT the wire envelope. Earlier this
    # test fed the outer wire envelope, and the construction
    # code silently coerced missing-fields to "" — the
    # `_coerce_message_dict` validator now rejects that shape
    # correctly. Update the input to match the dispatch contract.
    payload = {
        "id": "m1",
        "sender": "+1",
        "body": "hi",
        "received_at": "2026-01-01T00:00:00Z",
    }
    mgr._handle_message(payload)
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


def test_app_main_on_change_fans_out_to_js():
    """In the browser, the per-generation MessageManager's on_change callback
    fans the change out to JS subscribers via `App._dispatchChange()`.

    The coordinator reads config live at tick time, so the on_change
    callback does NOT call `apply_settings` — that work is pulled
    on the coordinator's next `tick()`. The JS fan-out is the only
    responsibility of the on_change path on the browser.

    History: pre-#48 the on_change lived in `app_main.py:_on_change_js`.
    The 2026-07-23 round-5 simplification moved it to
    `dashboard_runtime._on_change_js` (no per-generation discriminator
    — the runtime is built ONCE per page load; refresh to restart).
    The contract is unchanged: fan out to JS, never call apply_settings.

    We can't run PyScript in tests, so we read the source and assert
    the fan-out is present and that `apply_settings` is NOT in the
    on_change body.
    """
    p = (
        Path(__file__).parent.parent.parent
        / "heart-message-manager"
        / "dashboard_runtime.py"
    )
    src = p.read_text(encoding="utf-8")
    assert "def _on_change_js" in src, (
        "dashboard_runtime.py must define _on_change_js"
    )
    # Find the closure body — between `def _on_change_js():` (or
    # `def _on_change_js() -> None:`) and the next sibling `def `
    # at the same OR parent indent. `_on_change_js` is defined as
    # a nested closure inside `install_runtime()`.
    m = re.search(
        r"def _on_change_js\(\)[^:]*:\s*\n((?:[ \t]+.*\n|\s*\n)*?)(?=^[ \t]*def |^def |\Z)",
        src,
        re.MULTILINE,
    )
    assert m is not None, "could not extract _on_change_js body"
    body = m.group(1)
    # Drop the docstring.
    body = re.sub(r'"""[\s\S]*?"""', "", body, count=1)
    assert "_dispatchChange" in body, (
        "browser on_change callback must call app._dispatchChange to fan out to JS"
    )
    assert "apply_settings" not in body, (
        "browser on_change callback must not call _coordinator.apply_settings — "
        "the coordinator reads config live from message_manager.config"
    )


def test_create_proxy_is_invoked_with_callbacks():
    """Smoke check: the runtime wraps the per-callback closures in
    `create_proxy()` so PyScript can hand them to the JS side as
    JsProxies. Replaces the per-generation `_make_on_change_js`
    / `_on_envelope_js` / `_on_status_js` checks from before
    the 2026-07-23 round-5 simplification."""
    p = (
        Path(__file__).parent.parent.parent
        / "heart-message-manager"
        / "dashboard_runtime.py"
    )
    src = p.read_text(encoding="utf-8")
    assert "create_proxy(_on_change_js)" in src, (
        "dashboard_runtime.py must wrap the on_change closure in create_proxy() "
        "before handing it to MessageManager"
    )
    assert "create_proxy(_on_envelope_py)" in src, (
        "dashboard_runtime.py must wrap the envelope callback in create_proxy() "
        "before handing it to the MQTT-WS shim"
    )
    assert "create_proxy(_on_status_py)" in src, (
        "dashboard_runtime.py must wrap the status callback in create_proxy() "
        "before handing it to the MQTT-WS shim"
    )


# --- Scenario 3: MessageManager(coordinator=...) raises TypeError ------------


def test_message_manager_rejects_coordinator_kwarg():
    """The manager does not accept a `coordinator=` reference.

    The coordinator is captured by a closure (the `on_change` callback)
    — the manager itself does not need (or accept) a back-reference.
    """
    from lib_shared.effects_coordinator import EffectsCoordinator
    from lib_shared.models import EffectsSettings, TextSettings

    mgr = SimpleNamespace(
        messages=SimpleNamespace(get_messages=lambda limit=100, suppress=True: []),
        config=SimpleNamespace(effects_settings=EffectsSettings(), text_settings=TextSettings()),
    )
    mgr.get_effects_settings = lambda: mgr.config.effects_settings
    mgr.get_text_settings = lambda: mgr.config.text_settings
    coord = EffectsCoordinator(message_manager=mgr)
    with pytest.raises(TypeError, match="coordinator"):
        MessageManager(
            messages_api_url="",
            config_api_url="",
            api_key="",
            coordinator=coord,
        )
