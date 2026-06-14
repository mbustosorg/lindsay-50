"""Smoke test for the PyScript-side MqttWsClient wrapper.

The wrapper imports from the `js` module, which is only available
inside a Pyodide runtime. In CPython (the test environment) the
import fails — this test asserts the wrapper is the right *shape*:
the module loads (or fails for a known reason), and the class
exposes the documented constructor signature.

The JS shim itself (`mqtt_ws_client.js`) is exercised manually in
a browser via the verification scripts.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "heart-message-manager" / "static"))


# The wrapper imports from `js`, which doesn't exist in CPython.
# We stub it out for the import, then check the class signature.
class _JsProxy:
    """Stub for the Pyodide `js` module so we can import the wrapper."""


_js_stub = _JsProxy()


def _make_js_attr(name, value):
    setattr(_js_stub, name, value)


def _install_js_stub(monkeypatch):
    """Inject a fake `js` module into sys.modules so the wrapper imports."""
    import types

    fake_js = types.ModuleType("js")
    fake_js.createMqttWsClient = _make_js_attr("createMqttWsClient", None)
    fake_js.create_proxy = _make_js_attr("create_proxy", lambda x: x)
    monkeypatch.setitem(sys.modules, "js", fake_js)


def test_mqtt_ws_client_class_exists(monkeypatch):
    """The MqttWsClient class is importable and has a __init__ method."""
    _install_js_stub(monkeypatch)
    # Now import — the wrapper will use the fake js module
    if "mqtt_ws_client" in sys.modules:
        del sys.modules["mqtt_ws_client"]
    from mqtt_ws_client import MqttWsClient  # type: ignore[import-not-found]

    assert MqttWsClient is not None
    assert callable(MqttWsClient)
    assert inspect.isclass(MqttWsClient)


def test_mqtt_ws_client_init_signature(monkeypatch):
    """The MqttWsClient.__init__ exposes the documented kwargs."""
    _install_js_stub(monkeypatch)
    if "mqtt_ws_client" in sys.modules:
        del sys.modules["mqtt_ws_client"]
    from mqtt_ws_client import MqttWsClient  # type: ignore[import-not-found]

    sig = inspect.signature(MqttWsClient.__init__)
    params = list(sig.parameters.keys())
    # self, ws_url, username, password, topic, on_envelope, long_disconnect_ms
    assert "ws_url" in params
    assert "username" in params
    assert "password" in params
    assert "topic" in params
    assert "on_envelope" in params
    assert "long_disconnect_ms" in params


def test_mqtt_ws_client_has_start_and_close(monkeypatch):
    """The wrapper exposes start() and close() methods."""
    _install_js_stub(monkeypatch)
    if "mqtt_ws_client" in sys.modules:
        del sys.modules["mqtt_ws_client"]
    from mqtt_ws_client import MqttWsClient  # type: ignore[import-not-found]

    assert hasattr(MqttWsClient, "start")
    assert hasattr(MqttWsClient, "close")
    assert callable(MqttWsClient.start)
    assert callable(MqttWsClient.close)


def test_mqtt_ws_client_module_imports_cleanly(monkeypatch):
    """The module imports without raising (with the js stub in place)."""
    _install_js_stub(monkeypatch)
    if "mqtt_ws_client" in sys.modules:
        del sys.modules["mqtt_ws_client"]
    import mqtt_ws_client  # type: ignore[import-not-found]  # noqa: F401
