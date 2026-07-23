"""Tests for the /messages archive route (issue #48, §8).

The archive is a server-rendered, server-paginated list that is
INDEPENDENT of the dashboard's 100-record browser ring. The
pre-#48 contract is preserved:

  - /messages returns all canonical SQLite records beyond 100,
    newest first, in server-controlled pages of 50.
  - Pagination handles malformed, below-range, and beyond-final
    page values gracefully (clamps to the legal range).
  - Rows expose data-msg-id + data-received-at for any future
    automation.
  - No delete UI / APIs are exposed.
  - /messages does NOT load the simulated-Pi PyScript runtime or
    message-topic MQTT subscriber.

Reuses the harness from `test_messages_route.py` so the mock
fixture stack stays in one place.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

_HARNESS = _PROJECT_ROOT / "tests" / "test_messages_route.py"
_spec = importlib.util.spec_from_file_location("_messages_harness", str(_HARNESS))
_harness = importlib.util.module_from_spec(_spec)
sys.modules["_messages_harness"] = _harness
_spec.loader.exec_module(_harness)


def _make_messages(count, prefix="msg"):
    """Build `count` Message-shaped mocks with deterministic ids + received_at."""
    mocks = []
    for i in range(count):
        msg = MagicMock()
        msg.id = f"{prefix}-{i:03d}"
        msg.sender = "+15551234567"
        msg.body = f"body {i}"
        msg.received_at = f"2026-07-{20 - (i // 30)}-T10:00:00"
        msg.media = []
        msg.suppressed = False
        mocks.append(msg)
    return mocks


# --- §8.1: 50-row pagination, newest first -------------------------------


def _build_app_with_messages(messages, monkeypatch):
    """Build the harness app with `messages` as the sqlite list.

    The harness installs `sqlite` as a ModuleType in sys.modules
    inside `_load_app_module`. After loading, we patch the
    `get_all_messages` / `message_count` slots directly.

    `monkeypatch` is required: `_load_app_module` replaces the real
    `lib_shared.*` modules in `sys.modules` with MagicMocks so the
    test is hermetic. We use `monkeypatch.setitem` to register each
    captured real module so pytest's teardown restores them
    automatically — without this the next test's conftest teardown
    tries `from lib_shared.models import _default_effects_list`
    against the stub and errors.
    """

    class _PahoCtor:
        def __init__(self, dispatch_callback, **kwargs):
            self.publish_envelope = MagicMock(return_value=True)
            self.start = MagicMock()
            self.stop = MagicMock()

    # Capture real lib_shared.* modules BEFORE _load_app_module
    # replaces them with MagicMock stubs.
    for name in list(sys.modules):
        if name == "lib_shared" or name.startswith("lib_shared."):
            monkeypatch.setitem(sys.modules, name, sys.modules[name])

    monkeypatch.delenv("HEROKU_SLUG_COMMIT", raising=False)
    monkeypatch.delenv("EFFECTS_SETTINGS_OVERRIDE", raising=False)

    flask_app = _harness._load_app_module(_harness._make_mock_cfg(), _PahoCtor)
    sqlite_mod = sys.modules["sqlite"]
    sqlite_mod.get_all_messages = lambda: messages  # type: ignore[attr-defined]
    sqlite_mod.message_count = lambda: len(messages)  # type: ignore[attr-defined]
    return flask_app


def test_messages_returns_first_50_records(monkeypatch):
    messages = _make_messages(60)
    flask_app = _build_app_with_messages(messages, monkeypatch)
    c = flask_app.test_client()
    c.post("/login", data={"username": "admin", "password": "secret123"})

    response = c.get("/messages?page=1")
    assert response.status_code == 200
    body = response.data.decode()
    for i in range(50):
        assert f'data-msg-id="msg-{i:03d}"' in body, (
            f"msg-{i:03d} missing from page 1"
        )


def test_messages_page_2_returns_next_50(monkeypatch):
    messages = _make_messages(60)
    flask_app = _build_app_with_messages(messages, monkeypatch)
    c = flask_app.test_client()
    c.post("/login", data={"username": "admin", "password": "secret123"})

    response = c.get("/messages?page=2")
    assert response.status_code == 200
    body = response.data.decode()
    for i in range(50, 60):
        assert f'data-msg-id="msg-{i:03d}"' in body
    for i in range(50):
        assert f'data-msg-id="msg-{i:03d}"' not in body


# --- §8.2: malformed / beyond-final page handling ------------------------


def test_messages_page_below_range_clamps_to_one(monkeypatch):
    messages = _make_messages(10)
    flask_app = _build_app_with_messages(messages, monkeypatch)
    c = flask_app.test_client()
    c.post("/login", data={"username": "admin", "password": "secret123"})

    response = c.get("/messages?page=0")
    assert response.status_code == 200
    body = response.data.decode()
    for i in range(10):
        assert f'data-msg-id="msg-{i:03d}"' in body


def test_messages_page_beyond_final_renders_empty_slice(monkeypatch):
    messages = _make_messages(10)
    flask_app = _build_app_with_messages(messages, monkeypatch)
    c = flask_app.test_client()
    c.post("/login", data={"username": "admin", "password": "secret123"})

    response = c.get("/messages?page=999")
    assert response.status_code == 200
    body = response.data.decode()
    for i in range(10):
        assert f'data-msg-id="msg-{i:03d}"' not in body


def test_messages_malformed_page_value_does_not_500(monkeypatch):
    messages = _make_messages(10)
    flask_app = _build_app_with_messages(messages, monkeypatch)
    c = flask_app.test_client()
    c.post("/login", data={"username": "admin", "password": "secret123"})

    response = c.get("/messages?page=abc")
    assert response.status_code in (200, 302), (
        f"/messages?page=abc must not 500 (got {response.status_code}); "
        "malformed pagination values must degrade gracefully."
    )


# --- §8.3, §8.4: data attributes + no delete UI --------------------------


def test_messages_row_exposes_data_msg_id_and_received_at(monkeypatch):
    msg = MagicMock()
    msg.id = "msg-001"
    msg.sender = "+15551234567"
    msg.body = "hello"
    msg.received_at = "2026-07-20T10:00:00"
    msg.media = []
    msg.suppressed = False

    flask_app = _build_app_with_messages([msg], monkeypatch)
    c = flask_app.test_client()
    c.post("/login", data={"username": "admin", "password": "secret123"})

    response = c.get("/messages?page=1")
    body = response.data.decode()
    assert 'data-msg-id="msg-001"' in body
    assert 'data-received-at="2026-07-20T10:00:00"' in body


def test_messages_template_has_no_delete_controls():
    template = (
        _PROJECT_ROOT / "heart-message-manager" / "templates" / "messages.html"
    ).read_text(encoding="utf-8")
    assert "delete" not in template.lower(), (
        "messages.html must not contain delete UI — the spec §8.4 "
        "forbids permanent delete controls."
    )


def test_no_delete_message_route_in_main():
    main_src = (
        _PROJECT_ROOT / "heart-message-manager" / "main.py"
    ).read_text(encoding="utf-8")
    assert "/messages/<msg_id>/delete" not in main_src
    assert "def delete_message" not in main_src


# --- §8.5: /messages does not load simulator ----------------------------


def test_messages_route_does_not_set_dashboard_csp(monkeypatch):
    msg = MagicMock()
    msg.id = "msg-001"
    msg.sender = "+15551234567"
    msg.body = "hello"
    msg.received_at = "2026-07-20T10:00:00"
    msg.media = []
    msg.suppressed = False

    flask_app = _build_app_with_messages([msg], monkeypatch)
    c = flask_app.test_client()
    c.post("/login", data={"username": "admin", "password": "secret123"})

    response = c.get("/messages")
    csp = response.headers.get("Content-Security-Policy", "")
    assert "wasm-unsafe-eval" not in csp, (
        "/messages must not carry the PyScript/WASM CSP — the "
        "simulator doesn't load here (§8.5)."
    )
    assert "pyscript.net" not in csp


def test_messages_template_does_not_include_pyscript():
    template = (
        _PROJECT_ROOT / "heart-message-manager" / "templates" / "messages.html"
    ).read_text(encoding="utf-8")
    assert "<py-script" not in template, (
        "messages.html must not include <py-script> — the "
        "simulator doesn't load on /messages (§8.5)."
    )
    assert "pyscript.net" not in template