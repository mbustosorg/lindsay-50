"""Smoke test for the PyScript-side MessageBufferStore wrapper.

The wrapper imports from the `js` module, which is only available
inside a Pyodide runtime. In CPython (the test environment) the
import fails — this test asserts the wrapper is the right *shape*:
the module loads (with a `js` stub), and the class exposes the
documented constructor signature and methods.

The JS shim itself (`message_buffer_store.js`) is exercised manually
in a browser via the verification scripts.
"""

from __future__ import annotations

import inspect
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "heart-message-manager" / "static"))


def _install_js_stub(monkeypatch):
    """Inject a fake `js` module into sys.modules so the wrapper imports."""
    fake_js = types.ModuleType("js")
    fake_js.createMessageBufferStore = lambda opts: None
    monkeypatch.setitem(sys.modules, "js", fake_js)


def test_message_buffer_store_class_exists(monkeypatch):
    """The MessageBufferStore class is importable and has the right shape."""
    _install_js_stub(monkeypatch)
    if "message_buffer_store" in sys.modules:
        del sys.modules["message_buffer_store"]
    from message_buffer_store import MessageBufferStore  # type: ignore[import-not-found]

    assert MessageBufferStore is not None
    assert callable(MessageBufferStore)
    assert inspect.isclass(MessageBufferStore)


def test_message_buffer_store_init_signature(monkeypatch):
    """The MessageBufferStore.__init__ exposes the documented kwargs."""
    _install_js_stub(monkeypatch)
    if "message_buffer_store" in sys.modules:
        del sys.modules["message_buffer_store"]
    from message_buffer_store import MessageBufferStore  # type: ignore[import-not-found]

    sig = inspect.signature(MessageBufferStore.__init__)
    params = list(sig.parameters.keys())
    # self, db_name
    assert "db_name" in params


def test_message_buffer_store_has_required_methods(monkeypatch):
    """The wrapper exposes hydrate, wipe, put_message, and put_config."""
    _install_js_stub(monkeypatch)
    if "message_buffer_store" in sys.modules:
        del sys.modules["message_buffer_store"]
    from message_buffer_store import MessageBufferStore  # type: ignore[import-not-found]

    assert hasattr(MessageBufferStore, "hydrate")
    assert hasattr(MessageBufferStore, "wipe")
    assert hasattr(MessageBufferStore, "put_message")
    assert hasattr(MessageBufferStore, "put_config")
    for name in ("hydrate", "wipe", "put_message", "put_config"):
        assert callable(getattr(MessageBufferStore, name)), f"{name} is not callable"


def test_message_buffer_store_module_imports_cleanly(monkeypatch):
    """The module imports without raising (with the js stub in place)."""
    _install_js_stub(monkeypatch)
    if "message_buffer_store" in sys.modules:
        del sys.modules["message_buffer_store"]
    import message_buffer_store  # type: ignore[import-not-found]  # noqa: F401
