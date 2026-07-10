"""Tests for the refactored lib_shared/message_manager.py.

Covers the constructor signature, internal `_fetch` branching on `is_browser`,
async `seed()` flow, dispatch of message/config envelopes, filter rules,
ring-buffer eviction, and the AST guard that prevents the server/browser
imports from creeping back to module top.
"""

from __future__ import annotations

import ast
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Import the real lib_shared.message_manager at module load time so the
# AST/structural tests have a reference. The runtime tests look the
# module up fresh from sys.modules at test time (via `_mm`) and use
# the class from that live module — robust against sibling tests'
# autouse fixtures that wipe and re-import the `lib_shared.*` tree.
import lib_shared.message_manager  # noqa: E402
from lib_shared.models import FilterRule, SignConfig, SignSettings  # noqa: E402

PROJECT_ROOT = Path(__file__).parent.parent
MM_PATH = PROJECT_ROOT / "lib_shared" / "message_manager.py"


def _mm():
    """Return the live `lib_shared.message_manager` module.

    Other test files' autouse fixtures may wipe and re-import the
    `lib_shared.*` tree between tests. After a wipe, the new module
    in sys.modules is what `MessageManager._fetch` reads `_js_fetch`
    / `_requests` from — not the module captured at the top of this
    file. We always look up the module fresh so we can patch the
    live globals, then instantiate the class from that same module
    so its `__globals__` matches.
    """
    import importlib

    name = "lib_shared.message_manager"
    cached = sys.modules.get(name)
    if cached is None or not hasattr(cached, "MessageManager"):
        return importlib.import_module(name)
    return cached


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_env(raw_filter=None):
    """Return a fresh MessageEnvelope-style JSON string and the matching dict."""
    return json.dumps({"type": "message", "payload": raw_filter})


def _make_config_env(raw):
    return json.dumps({"type": "config", "payload": raw})


@pytest.fixture
def messages_api_url() -> str:
    return "http://localhost/api/messages"


@pytest.fixture
def config_api_url() -> str:
    return "http://localhost/api/config"


@pytest.fixture
def api_key() -> str:
    return "device-api-key"


@pytest.fixture
def seed_messages():
    return [
        {"id": "m1", "sender": "+15551111111", "body": "hello", "received_at": "2026-06-01T10:00:00Z"},
        {"id": "m2", "sender": "+15552222222", "body": "world", "received_at": "2026-06-01T11:00:00Z"},
    ]


@pytest.fixture
def seed_config():
    return {
        "filters": [],
        "senders": [],
        "effects_settings": {
            "effects": [{"name": "Hyperspace", "enabled": True}],
            "fade_seconds": 2.0,
            "hold_seconds": 15.0,
            "intro_seconds": 5.0,
            "idle_seconds": 300.0,
            "recent_count": 5,
        },
        "text_settings": {
            "speed": 3,
            "color": 16711680,
            "text_effect": "scroll",
        },
        "sign": {"name": "Test Sign"},
        "timezone": "US/Pacific",
        "version": 2,
    }


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_constructor_accepts_required_kwargs(self, messages_api_url, config_api_url, api_key):
        """Constructor accepts (messages_api_url, config_api_url, api_key, is_browser, on_change)."""
        mm = _mm()
        mgr = mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
        )
        assert mgr._messages_api_url == messages_api_url
        assert mgr._config_api_url == config_api_url
        assert mgr._api_key == api_key
        # is_browser defaults to False (the device's value)
        assert mgr._is_browser is False
        # No callback by default
        assert mgr._on_change is None
        # Public surface exposed
        assert mgr.config is not None
        assert mgr.messages is not None
        # Ring buffer maxlen == 100
        assert mgr.messages._msgs.maxlen == 100

    def test_constructor_with_is_browser_true(self, messages_api_url, config_api_url, api_key):
        """is_browser=True flag is stored and drives the internal _fetch branch."""
        mm = _mm()
        mgr = mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            is_browser=True,
        )
        assert mgr._is_browser is True

    def test_constructor_with_on_change(self, messages_api_url, config_api_url, api_key):
        """on_change callback is stored (parameterless)."""
        cb = MagicMock()
        mm = _mm()
        mgr = mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_change=cb,
        )
        assert mgr._on_change is cb


# ---------------------------------------------------------------------------
# seed() — calls the internal _fetch for both URLs, in order
# ---------------------------------------------------------------------------


class TestSeedServer:
    def test_seed_calls_fetch_for_messages_then_config(
        self, messages_api_url, config_api_url, api_key, seed_messages, seed_config
    ):
        """seed() awaits _fetch for both URLs in order (server path)."""
        mm = _mm()
        mgr = mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            is_browser=False,
        )
        call_log = []

        async def mock_fetch(url):
            call_log.append(url)
            if url == messages_api_url:
                return seed_messages
            if url == config_api_url:
                return seed_config
            raise AssertionError(f"unexpected url: {url}")

        mgr._fetch = mock_fetch  # type: ignore[assignment]
        asyncio.run(mgr.seed())
        assert call_log == [messages_api_url, config_api_url]

    def test_seed_populates_messages_and_config(
        self, messages_api_url, config_api_url, api_key, seed_messages, seed_config
    ):
        """After seed(), the ring buffer and SignConfig are populated."""
        mm = _mm()
        mgr = mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            is_browser=False,
        )

        async def mock_fetch(url):
            return seed_messages if url == messages_api_url else seed_config

        mgr._fetch = mock_fetch  # type: ignore[assignment]
        asyncio.run(mgr.seed())

        msgs = mgr.get_messages(limit=10, suppress=False)
        assert len(msgs) == 2
        # Newest first
        assert msgs[0].message.id == "m2"
        assert msgs[1].message.id == "m1"
        # Config is populated
        assert mgr.config.timezone == "US/Pacific"
        assert mgr.config.sign.name == "Test Sign"


class TestSeedBrowser:
    def test_seed_calls_fetch_for_messages_then_config_browser(
        self, messages_api_url, config_api_url, api_key, seed_messages, seed_config
    ):
        """seed() awaits _fetch for both URLs in order (browser path)."""
        mm = _mm()
        mgr = mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            is_browser=True,
        )
        call_log = []

        async def mock_fetch(url):
            call_log.append(url)
            if url == messages_api_url:
                return seed_messages
            if url == config_api_url:
                return seed_config
            raise AssertionError(f"unexpected url: {url}")

        mgr._fetch = mock_fetch  # type: ignore[assignment]
        asyncio.run(mgr.seed())
        assert call_log == [messages_api_url, config_api_url]


# ---------------------------------------------------------------------------
# _fetch — server path (is_browser=False) uses requests via asyncio.to_thread
# ---------------------------------------------------------------------------


class TestFetchServerPath:
    def test_fetch_uses_requests_in_a_worker_thread(self, messages_api_url, config_api_url, api_key):
        """Server path lazily imports requests and calls .get via asyncio.to_thread."""
        # Mock the requests module
        mock_requests = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True}
        mock_response.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_response

        mm = _mm()
        mm._requests = mock_requests
        try:
            mgr = mm.MessageManager(
                messages_api_url=messages_api_url,
                config_api_url=config_api_url,
                api_key=api_key,
                is_browser=False,
            )
            result = asyncio.run(mgr._fetch(messages_api_url))
            assert result == {"ok": True}
            # requests.get was called with the right args
            mock_requests.get.assert_called_once()
            args, kwargs = mock_requests.get.call_args
            assert args[0] == messages_api_url
            assert kwargs["headers"]["X-API-Key"] == api_key
            assert kwargs["timeout"] == 5
            # raise_for_status was called
            mock_response.raise_for_status.assert_called_once()
        finally:
            mm._requests = None  # reset for other tests

    def test_fetch_server_raises_on_http_error(self, messages_api_url, config_api_url, api_key):
        """Server path raises on HTTP errors (raise_for_status bubbles up)."""
        mock_requests = MagicMock()
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = Exception("500 Server Error")
        mock_requests.get.return_value = mock_response

        mm = _mm()
        mm._requests = mock_requests
        try:
            mgr = mm.MessageManager(
                messages_api_url=messages_api_url,
                config_api_url=config_api_url,
                api_key=api_key,
                is_browser=False,
            )
            with pytest.raises(Exception, match="500 Server Error"):
                asyncio.run(mgr._fetch(messages_api_url))
        finally:
            mm._requests = None


# ---------------------------------------------------------------------------
# _fetch — browser path (is_browser=True) uses js.fetch
# ---------------------------------------------------------------------------


class TestFetchBrowserPath:
    def test_fetch_uses_js_fetch_with_api_key_header(self, messages_api_url, config_api_url, api_key):
        """Browser path lazily imports js.fetch and calls it with the X-API-Key header.

        The headers are converted to a JS object via
        `Object.fromEntries(to_js([['X-API-Key', api_key]]))`
        because `RequestInit.headers` rejects a bare Python
        dict (Pyodide JsProxy) at the property-access layer —
        see message_manager.py for the full note.
        """
        # Build a mock response: .ok = True, .json() returns a coroutine
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status = 200

        async def _json_coro():
            return {"ok": True}

        mock_response.json = _json_coro

        # Sentinel headers object the test can recognize.
        sentinel_headers = object()

        # Mock helpers: `to_js` and `Object.fromEntries` are
        # module-globals; capture the array passed to fromEntries
        # so the test can assert on the underlying key/value.
        from_entries_calls = []
        to_js_calls = []

        def _fake_to_js(pairs):
            to_js_calls.append(pairs)
            return pairs  # identity — we read it back below

        def _fake_from_entries(js_pairs):
            from_entries_calls.append(js_pairs)
            return sentinel_headers

        # Build a mock fetch: returns the response directly (no need to await the call)
        async def _fetch_call(url, method=None, headers=None):
            assert url == messages_api_url
            assert method == "GET"
            # The headers arg is the result of `js.Object.fromEntries(...)`,
            # which our mock returns as the sentinel — and the API key
            # reached it via the converted `[["X-API-Key", api_key]]` list.
            assert headers is sentinel_headers
            assert to_js_calls == [[["X-API-Key", api_key]]]
            assert from_entries_calls == [[["X-API-Key", api_key]]]
            return mock_response

        mm = _mm()
        mm._js_fetch = _fetch_call
        mm._js_object_from_entries = _fake_from_entries
        mm._to_js = _fake_to_js
        try:
            mgr = mm.MessageManager(
                messages_api_url=messages_api_url,
                config_api_url=config_api_url,
                api_key=api_key,
                is_browser=True,
            )
            result = asyncio.run(mgr._fetch(messages_api_url))
            assert result == {"ok": True}
        finally:
            mm._js_fetch = None  # reset for other tests
            mm._js_object_from_entries = None
            mm._to_js = None

    def test_fetch_browser_raises_on_non_ok_response(self, messages_api_url, config_api_url, api_key):
        """Browser path raises RuntimeError on non-ok response."""
        mock_response = MagicMock()
        mock_response.ok = False
        mock_response.status = 401

        async def _fetch_call(url, method=None, headers=None):
            return mock_response

        def _fake_to_js(pairs):
            return pairs

        def _fake_from_entries(_js_pairs):
            return object()

        mm = _mm()
        mm._js_fetch = _fetch_call
        mm._js_object_from_entries = _fake_from_entries
        mm._to_js = _fake_to_js
        try:
            mgr = mm.MessageManager(
                messages_api_url=messages_api_url,
                config_api_url=config_api_url,
                api_key=api_key,
                is_browser=True,
            )
            with pytest.raises(RuntimeError, match="returned HTTP 401"):
                asyncio.run(mgr._fetch(messages_api_url))
        finally:
            mm._js_fetch = None
            mm._js_object_from_entries = None
            mm._to_js = None


# ---------------------------------------------------------------------------
# dispatch — message and config envelopes, filter rules, on_change event
# ---------------------------------------------------------------------------


class TestDispatchMessage:
    def test_dispatch_message_envelope_routes_to_ring(self, messages_api_url, config_api_url, api_key):
        """A type=message envelope is added to the ring buffer and on_change is invoked."""
        cb = MagicMock()
        mm = _mm()
        mgr = mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_change=cb,
        )
        env = _make_env(
            {
                "id": "x1",
                "sender": "+15551234567",
                "body": "hi",
                "received_at": "2026-06-01T12:00:00Z",
            }
        )
        mgr.dispatch(env)
        msgs = mgr.get_messages(limit=10, suppress=False)
        assert len(msgs) == 1
        assert msgs[0].message.id == "x1"
        assert msgs[0].message.body == "hi"
        # on_change fires once after the buffer write
        cb.assert_called_once()
        # The callback is parameterless — listeners re-read state
        cb_arg = cb.call_args[0]
        assert cb_arg == ()

    def test_dispatch_message_envelope_preserves_media(self, messages_api_url, config_api_url, api_key):
        """Issue #38: the `media` field on the wire envelope round-trips to
        the in-memory Message. Without this, the coordinator's
        `BrowserMediaOverlay` (preview) / `MediaCycler` (Pi) sees
        `media=[]` on every picked message and the image never
        renders — even though Flask published the right envelope.
        Regression test for the `_handle_message` drop."""
        cb = MagicMock()
        mm = _mm()
        mgr = mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_change=cb,
        )
        env = _make_env(
            {
                "id": "m1",
                "sender": "+15551234567",
                "body": "mms",
                "received_at": "2026-06-01T12:00:00Z",
                "media": [
                    {"type": "image/jpeg", "url": "media/images/2026-07/a.jpg"},
                    {"type": "image/png", "url": "media/images/2026-07/b.png"},
                ],
            }
        )
        mgr.dispatch(env)
        msgs = mgr.get_messages(limit=10, suppress=False)
        assert len(msgs) == 1
        assert msgs[0].message.media == [
            {"type": "image/jpeg", "url": "media/images/2026-07/a.jpg"},
            {"type": "image/png", "url": "media/images/2026-07/b.png"},
        ]

    def test_dispatch_message_envelope_absent_media_defaults_to_empty(
        self,
        messages_api_url,
        config_api_url,
        api_key,
    ):
        """SMS-only envelopes (no `media` key) map to `media=[]` on the
        Message. The defensive `payload.get("media") or []` collapses
        both "missing key" and "explicit None" to the same default."""
        cb = MagicMock()
        mm = _mm()
        mgr = mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_change=cb,
        )
        env = _make_env(
            {
                "id": "m2",
                "sender": "+15551234567",
                "body": "sms only",
                "received_at": "2026-06-01T12:00:00Z",
                # no `media` key
            }
        )
        mgr.dispatch(env)
        msgs = mgr.get_messages(limit=10, suppress=False)
        assert len(msgs) == 1
        assert msgs[0].message.media == []

    def test_dispatch_invokes_on_change_on_config(self, messages_api_url, config_api_url, api_key):
        """A type=config envelope updates the config and on_change IS invoked."""
        cb = MagicMock()
        mm = _mm()
        mgr = mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_change=cb,
        )
        env = _make_config_env(
            {
                "filters": [],
                "senders": [],
                "effects_settings": {
                    "effects": [
                        {"name": "Hyperspace", "enabled": True},
                        {"name": "Fireworks", "enabled": True},
                    ],
                    "fade_seconds": 2.0,
                    "hold_seconds": 15.0,
                    "intro_seconds": 5.0,
                    "idle_seconds": 300.0,
                    "recent_count": 5,
                },
                "text_settings": {
                    "speed": 3,
                    "color": 16711680,
                    "text_effect": "scroll",
                },
                "sign": {"name": "Updated"},
                "timezone": "US/Pacific",
                "version": 2,
            }
        )
        mgr.dispatch(env)
        # Config updated
        assert mgr.config.sign.name == "Updated"
        names = [e["name"] for e in mgr.config.effects_settings.effects]
        assert "Fireworks" in names
        # on_change WAS called (the universal change event covers
        # both message arrivals and config updates)
        cb.assert_called_once()

    def test_dispatch_malformed_envelope_is_dropped(self, messages_api_url, config_api_url, api_key):
        """A malformed envelope is dropped without raising."""
        cb = MagicMock()
        mm = _mm()
        mgr = mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_change=cb,
        )
        # Not valid JSON
        mgr.dispatch("not json")
        # Valid JSON but not an envelope shape
        mgr.dispatch(json.dumps({"foo": "bar"}))
        # No state changes, no callback
        assert mgr.get_messages(limit=10, suppress=False) == []
        cb.assert_not_called()

    def test_dispatch_unknown_envelope_type_dropped(self, messages_api_url, config_api_url, api_key):
        """An envelope with an unknown type is dropped."""
        mm = _mm()
        mgr = mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
        )
        env = json.dumps({"type": "spam", "payload": {}})
        mgr.dispatch(env)
        assert mgr.get_messages(limit=10, suppress=False) == []


# ---------------------------------------------------------------------------
# dispatch — type=command envelopes (handler dispatch)
# ---------------------------------------------------------------------------


class TestDispatchCommand:
    """Tests for the v2 type=command envelope branch.

    The dispatch logic is a small switch on `payload["action"]` —
    the only supported action is `check-for-update`, which calls
    the parameterless `on_check_for_update` callback passed to
    the constructor. Unknown actions, missing handlers, and
    malformed payloads are logged + dropped. MessageManager has
    no built-in command handlers — the matrix controller passes
    its `check_for_update` handler as a constructor kwarg.
    """

    def test_command_envelope_round_trips_through_json(self):
        """MessageEnvelope("command", {"action": "check-for-update"}) round-trips via from_json."""
        from lib_shared.models import MessageEnvelope

        env = MessageEnvelope("command", {"action": "check-for-update"})
        raw = env.to_json()
        assert raw == '{"type":"command","payload":{"action":"check-for-update"}}'
        restored = MessageEnvelope.from_json(raw)
        assert restored.type == "command"
        assert restored.payload == {"action": "check-for-update"}

    def test_dispatch_command_invokes_on_check_for_update(self, messages_api_url, config_api_url, api_key):
        """A type=command envelope with action=check-for-update calls the callback parameterless."""
        handler = MagicMock()
        mgr = _mm().MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_check_for_update=handler,
        )
        env = json.dumps({"type": "command", "payload": {"action": "check-for-update"}})
        mgr.dispatch(env)
        handler.assert_called_once_with()

    def test_dispatch_command_unknown_action_is_dropped(self, messages_api_url, config_api_url, api_key):
        """Unknown actions: no handler invocation; the envelope is dropped."""
        handler = MagicMock()
        mgr = _mm().MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_check_for_update=handler,
        )
        env = json.dumps({"type": "command", "payload": {"action": "dance"}})
        mgr.dispatch(env)
        handler.assert_not_called()

    def test_dispatch_command_missing_payload_is_dropped(self, messages_api_url, config_api_url, api_key):
        """A command envelope with payload=None is dropped without side effects."""
        handler = MagicMock()
        mgr = _mm().MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_check_for_update=handler,
        )
        env = json.dumps({"type": "command", "payload": None})
        mgr.dispatch(env)
        handler.assert_not_called()

    def test_dispatch_command_missing_action_key_is_dropped(self, messages_api_url, config_api_url, api_key):
        """A command envelope with no 'action' key is dropped."""
        handler = MagicMock()
        mgr = _mm().MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_check_for_update=handler,
        )
        env = json.dumps({"type": "command", "payload": {}})
        mgr.dispatch(env)
        handler.assert_not_called()

    def test_dispatch_command_non_string_action_is_dropped(self, messages_api_url, config_api_url, api_key):
        """Non-string action values are dropped (no handler invocation)."""
        handler = MagicMock()
        mgr = _mm().MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_check_for_update=handler,
        )
        env = json.dumps({"type": "command", "payload": {"action": 42}})
        mgr.dispatch(env)
        handler.assert_not_called()

    def test_dispatch_command_non_dict_payload_is_dropped(self, messages_api_url, config_api_url, api_key):
        """A command envelope with payload='reboot' (string) is dropped — only dicts are valid."""
        handler = MagicMock()
        mgr = _mm().MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_check_for_update=handler,
        )
        env = json.dumps({"type": "command", "payload": "reboot"})
        mgr.dispatch(env)
        handler.assert_not_called()

    def test_dispatch_command_without_callback_drops_with_warning(self, messages_api_url, config_api_url, api_key):
        """check-for-update with no callback is dropped (not raised)."""
        mgr = _mm().MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
        )
        env = json.dumps({"type": "command", "payload": {"action": "check-for-update"}})
        # Must not raise
        mgr.dispatch(env)

    def test_dispatch_command_handler_exception_is_swallowed(self, messages_api_url, config_api_url, api_key):
        """A handler that raises does not crash the paho network thread."""

        def boom():
            raise RuntimeError("listener bug")

        mgr = _mm().MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_check_for_update=boom,
        )
        env = json.dumps({"type": "command", "payload": {"action": "check-for-update"}})
        # Must not raise
        mgr.dispatch(env)

    def test_dispatch_message_still_routes_after_command_handler_set(self, messages_api_url, config_api_url, api_key):
        """Regression: type=message envelope still routes to the ring buffer."""
        handler = MagicMock()
        mgr = _mm().MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_check_for_update=handler,
        )
        env = _make_env(
            {
                "id": "reg-1",
                "sender": "+15551234567",
                "body": "still works",
                "received_at": "2026-06-01T12:00:00Z",
            }
        )
        mgr.dispatch(env)
        msgs = mgr.get_messages(limit=10, suppress=False)
        assert len(msgs) == 1
        assert msgs[0].message.id == "reg-1"
        assert msgs[0].message.body == "still works"
        # The command handler was not invoked for a message envelope.
        handler.assert_not_called()

    def test_dispatch_config_still_routes_after_command_handler_set(self, messages_api_url, config_api_url, api_key):
        """Regression: type=config envelope still updates the SignConfig."""
        handler = MagicMock()
        mgr = _mm().MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_check_for_update=handler,
        )
        env = _make_config_env(
            {
                "filters": [],
                "senders": [],
                "effects_settings": {
                    "effects": [{"name": "Fireworks", "enabled": True}],
                    "fade_seconds": 2.0,
                    "hold_seconds": 15.0,
                    "intro_seconds": 5.0,
                    "idle_seconds": 300.0,
                    "recent_count": 5,
                },
                "text_settings": {
                    "speed": 3,
                    "color": 16711680,
                    "text_effect": "scroll",
                },
                "sign": {"name": "Reg Sign"},
                "timezone": "US/Pacific",
                "version": 2,
            }
        )
        mgr.dispatch(env)
        assert mgr.config.sign.name == "Reg Sign"
        handler.assert_not_called()


# ---------------------------------------------------------------------------
# Filter rules
# ---------------------------------------------------------------------------


class TestDispatchFilterRules:
    def test_filtered_message_invokes_on_change(self, messages_api_url, config_api_url, api_key):
        """A message matching a filter rule is added; on_change fires.

        The old per-message callback skipped filtered messages. The
        new universal `on_change` fires for every state change —
        the suppression flag is computed at read time, so a
        listener that cares re-reads with `get_messages(suppress=True)`.
        """
        cb = MagicMock()
        mm = _mm()
        mgr = mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_change=cb,
        )
        mgr.config.filters.append(FilterRule(type="keyword", pattern="spam", action="suppress"))
        env = _make_env(
            {
                "id": "x1",
                "sender": "+15551234567",
                "body": "this is spam",
                "received_at": "2026-06-01T12:00:00Z",
            }
        )
        mgr.dispatch(env)
        # Message is in the ring (with suppressed=True)
        msgs_all = mgr.get_messages(limit=10, suppress=False)
        assert len(msgs_all) == 1
        assert msgs_all[0].suppressed is True
        # on_change WAS called (universal change event covers all writes)
        cb.assert_called_once()

    def test_non_filtered_message_invokes_on_change(self, messages_api_url, config_api_url, api_key):
        """A non-matching message invokes on_change and is not suppressed."""
        cb = MagicMock()
        mm = _mm()
        mgr = mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_change=cb,
        )
        mgr.config.filters.append(FilterRule(type="keyword", pattern="spam", action="suppress"))
        env = _make_env(
            {
                "id": "x1",
                "sender": "+15551234567",
                "body": "hello world",
                "received_at": "2026-06-01T12:00:00Z",
            }
        )
        mgr.dispatch(env)
        cb.assert_called_once()
        msgs_all = mgr.get_messages(limit=10, suppress=False)
        assert msgs_all[0].suppressed is False


# ---------------------------------------------------------------------------
# on_change — universal change event
# ---------------------------------------------------------------------------


class TestOnChange:
    def test_handle_message_emits_change(self, messages_api_url, config_api_url, api_key):
        """`_handle_message` invokes the parameterless on_change callback once per write."""
        cb = MagicMock()
        mm = _mm()
        mgr = mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_change=cb,
        )
        mgr._handle_message(
            {
                "id": "m1",
                "sender": "+15551234567",
                "body": "hi",
                "received_at": "2026-06-01T12:00:00Z",
            }
        )
        cb.assert_called_once_with()
        # Listener is parameterless — no args
        assert cb.call_args.args == ()

    def test_handle_message_suppressed_still_emits_change(self, messages_api_url, config_api_url, api_key):
        """Suppressed messages still fire on_change — suppression is computed at read time."""
        cb = MagicMock()
        mm = _mm()
        mgr = mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_change=cb,
        )
        mgr.config.filters.append(FilterRule(type="keyword", pattern="spam", action="suppress"))
        mgr._handle_message(
            {
                "id": "m1",
                "sender": "+15551234567",
                "body": "this is spam",
                "received_at": "2026-06-01T12:00:00Z",
            }
        )
        cb.assert_called_once()
        # The message is in the ring, with suppressed=True
        msgs = mgr.get_messages(limit=10, suppress=False)
        assert len(msgs) == 1
        assert msgs[0].suppressed is True

    def test_handle_config_emits_change(self, messages_api_url, config_api_url, api_key):
        """`_handle_config` invokes on_change after updating the SignConfig."""
        cb = MagicMock()
        mm = _mm()
        mgr = mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_change=cb,
        )
        mgr._handle_config(
            {
                "filters": [],
                "senders": [],
                "effects_settings": {
                    "effects": [{"name": "Fireworks", "enabled": True}],
                    "fade_seconds": 2.0,
                    "hold_seconds": 15.0,
                    "intro_seconds": 5.0,
                    "idle_seconds": 300.0,
                    "recent_count": 5,
                },
                "text_settings": {
                    "speed": 3,
                    "color": 16711680,
                    "text_effect": "scroll",
                },
                "sign": {"name": "Updated"},
                "timezone": "US/Pacific",
                "version": 2,
            }
        )
        cb.assert_called_once()
        assert mgr.config.sign.name == "Updated"

    def test_seed_emits_change_once(self, messages_api_url, config_api_url, api_key, seed_messages, seed_config):
        """A successful seed of both endpoints fires on_change exactly once."""
        cb = MagicMock()
        mm = _mm()
        mgr = mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_change=cb,
        )

        async def mock_fetch(url):
            return seed_messages if url == messages_api_url else seed_config

        mgr._fetch = mock_fetch  # type: ignore[assignment]
        asyncio.run(mgr.seed())
        cb.assert_called_once()
        # And the buffer is populated
        assert len(mgr.get_messages(limit=10, suppress=False)) == 2

    def test_partial_seed_still_emits_change(self, messages_api_url, config_api_url, api_key, seed_messages):
        """If only one endpoint succeeds, on_change still fires once."""
        cb = MagicMock()
        mm = _mm()
        mgr = mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_change=cb,
        )

        async def mock_fetch(url):
            if url == messages_api_url:
                return seed_messages
            raise RuntimeError("config endpoint down")

        mgr._fetch = mock_fetch  # type: ignore[assignment]
        asyncio.run(mgr.seed())
        # Buffer populated, config not, but on_change still fired
        cb.assert_called_once()
        assert len(mgr.get_messages(limit=10, suppress=False)) == 2

    def test_swallowed_callback_exception(self, messages_api_url, config_api_url, api_key):
        """A faulty callback must not break the buffer write."""

        def boom():
            raise RuntimeError("listener bug")

        mm = _mm()
        mgr = mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_change=boom,
        )
        mgr._handle_message(
            {
                "id": "m1",
                "sender": "+15551234567",
                "body": "hi",
                "received_at": "2026-06-01T12:00:00Z",
            }
        )
        # The message still made it into the buffer despite the
        # listener raising.
        msgs = mgr.get_messages(limit=10, suppress=False)
        assert len(msgs) == 1
        assert msgs[0].message.id == "m1"

    def test_get_messages_with_suppress_true_excludes_suppressed(self, messages_api_url, config_api_url, api_key):
        """get_messages(suppress=True) excludes suppressed messages."""
        mm = _mm()
        mgr = mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
        )
        mgr.config.filters.append(FilterRule(type="keyword", pattern="spam", action="suppress"))
        # A non-spam message
        mgr.dispatch(
            _make_env(
                {
                    "id": "a",
                    "sender": "+1",
                    "body": "hello",
                    "received_at": "2026-06-01T10:00:00Z",
                }
            )
        )
        # A spam message
        mgr.dispatch(
            _make_env(
                {
                    "id": "b",
                    "sender": "+1",
                    "body": "spam here",
                    "received_at": "2026-06-01T11:00:00Z",
                }
            )
        )
        # suppress=True excludes "b"
        kept = mgr.get_messages(limit=10, suppress=True)
        assert len(kept) == 1
        assert kept[0].message.id == "a"
        # suppress=False includes both
        all_msgs = mgr.get_messages(limit=10, suppress=False)
        assert len(all_msgs) == 2


# ---------------------------------------------------------------------------
# Ring buffer eviction at 101 entries
# ---------------------------------------------------------------------------


class TestSessionCache:
    """sessionStorage cache (browser-only).

    The browser's sessionStorage persists the in-memory
    state across full-page navigations within a tab so
    /testing and /preview can re-render from cache instead
    of waiting for a network re-seed on every nav. The
    Pi doesn't have sessionStorage; all cache methods are
    no-ops on the server path.
    """

    @staticmethod
    def _make_session_storage_shim():
        """Return a (storage_dict, shim_callable) pair.

        storage_dict is a plain Python dict. The shim mimics
        `js.sessionStorage` with `getItem`/`setItem`/`removeItem`
        that read/write that dict, plus a `key` for iteration
        (used by the login-page wipe logic, not by
        MessageManager itself).
        """
        store: dict = {}

        def getItem(k):
            return store.get(k)

        def setItem(k, v):
            store[k] = v

        def removeItem(k):
            store.pop(k, None)

        def key(i):
            return list(store.keys())[i] if 0 <= i < len(store) else None

        shim = MagicMock()
        shim.getItem = getItem
        shim.setItem = setItem
        shim.removeItem = removeItem
        shim.key = key
        shim._store = store
        shim.length = lambda: len(store)
        return store, shim

    @staticmethod
    def _make_json_shim():
        """Return a shim that mimics `js.JSON` (stringify + parse)."""
        shim = MagicMock()
        shim.stringify = json.dumps
        shim.parse = json.loads
        return shim

    @staticmethod
    def _make_to_js_shim():
        """Identity shim for `pyodide.ffi.to_js`.

        The real `to_js` converts Python dicts to JsProxies that
        `JSON.stringify` can walk. Under the test shim the payload
        is already a plain Python dict, so an identity conversion
        is faithful: the stringifier still walks it correctly and
        the bytes-on-the-wire match a real Pyodide round-trip.
        """
        return lambda obj, dict_converter=None: obj

    @staticmethod
    def _make_from_entries_shim():
        """Identity shim for `js.Object.fromEntries`.

        Same rationale as `_make_to_js_shim`: the Python dict is
        already structured correctly, so we just hand it back.
        """
        return lambda entries: dict(entries) if hasattr(entries, "__iter__") else entries

    def test_hydrate_from_cache_empty_returns_false(self, messages_api_url, config_api_url, api_key):
        """No cache entry → returns False; on_change not fired."""
        cb = MagicMock()
        _, ss_shim = self._make_session_storage_shim()
        json_shim = self._make_json_shim()
        mm = _mm()
        mm._js_session_storage = ss_shim
        mm._js_json = json_shim
        mm._to_js = self._make_to_js_shim()
        mm._js_object_from_entries = self._make_from_entries_shim()
        try:
            mgr = mm.MessageManager(
                messages_api_url=messages_api_url,
                config_api_url=config_api_url,
                api_key=api_key,
                is_browser=True,
                on_change=cb,
            )
            hit = asyncio.run(mgr.hydrate_from_cache())
            assert hit is False
            cb.assert_not_called()
        finally:
            mm._js_session_storage = None
            mm._js_json = None
            mm._to_js = None
            mm._js_object_from_entries = None

    def test_hydrate_from_cache_invalidates_on_version_mismatch(self, messages_api_url, config_api_url, api_key):
        """Cache with wrong `v` → returns False; on_change not fired."""
        store, ss_shim = self._make_session_storage_shim()
        json_shim = self._make_json_shim()
        # Pre-populate with a v=0 entry
        store["lindsay50:seed:v1:Lindsay's Heart"] = json.dumps(
            {
                "v": 0,
                "sign_name": "Lindsay's Heart",
                "messages": [],
                "config": {},
            }
        )
        cb = MagicMock()
        mm = _mm()
        mm._js_session_storage = ss_shim
        mm._js_json = json_shim
        mm._to_js = self._make_to_js_shim()
        mm._js_object_from_entries = self._make_from_entries_shim()
        try:
            mgr = mm.MessageManager(
                messages_api_url=messages_api_url,
                config_api_url=config_api_url,
                api_key=api_key,
                is_browser=True,
                on_change=cb,
            )
            hit = asyncio.run(mgr.hydrate_from_cache())
            assert hit is False
            cb.assert_not_called()
        finally:
            mm._js_session_storage = None
            mm._js_json = None
            mm._to_js = None
            mm._js_object_from_entries = None

    def test_hydrate_from_cache_invalidates_on_sign_mismatch(self, messages_api_url, config_api_url, api_key):
        """Cache for a different sign → returns False; on_change not fired."""
        store, ss_shim = self._make_session_storage_shim()
        json_shim = self._make_json_shim()
        store["lindsay50:seed:v1:Lindsay's Heart"] = json.dumps(
            {
                "v": 1,
                "sign_name": "Different Sign",
                "messages": [],
                "config": {},
            }
        )
        cb = MagicMock()
        mm = _mm()
        mm._js_session_storage = ss_shim
        mm._js_json = json_shim
        mm._to_js = self._make_to_js_shim()
        mm._js_object_from_entries = self._make_from_entries_shim()
        try:
            mgr = mm.MessageManager(
                messages_api_url=messages_api_url,
                config_api_url=config_api_url,
                api_key=api_key,
                is_browser=True,
                on_change=cb,
            )
            hit = asyncio.run(mgr.hydrate_from_cache())
            assert hit is False
            cb.assert_not_called()
        finally:
            mm._js_session_storage = None
            mm._js_json = None
            mm._to_js = None
            mm._js_object_from_entries = None

    def test_hydrate_from_cache_invalidates_on_corrupt_json(self, messages_api_url, config_api_url, api_key):
        """Invalid JSON in cache → returns False; no exception propagates."""
        _, ss_shim = self._make_session_storage_shim()
        json_shim = self._make_json_shim()
        # json_shim.parse is json.loads; force it to raise for our marker
        json_shim.parse = MagicMock(side_effect=ValueError("bad json"))
        cb = MagicMock()
        mm = _mm()
        mm._js_session_storage = ss_shim
        mm._js_json = json_shim
        mm._to_js = self._make_to_js_shim()
        mm._js_object_from_entries = self._make_from_entries_shim()
        try:
            mgr = mm.MessageManager(
                messages_api_url=messages_api_url,
                config_api_url=config_api_url,
                api_key=api_key,
                is_browser=True,
                on_change=cb,
            )
            hit = asyncio.run(mgr.hydrate_from_cache())
            assert hit is False
            cb.assert_not_called()
        finally:
            mm._js_session_storage = None
            mm._js_json = None
            mm._to_js = None
            mm._js_object_from_entries = None

    def test_hydrate_from_cache_populates_messages_and_config(
        self, messages_api_url, config_api_url, api_key, seed_messages, seed_config
    ):
        """Round-trip: write cache, fresh manager, hydrate → messages + config match."""
        store, ss_shim = self._make_session_storage_shim()
        json_shim = self._make_json_shim()
        # First manager: populate, write cache
        mm = _mm()
        mm._js_session_storage = ss_shim
        mm._js_json = json_shim
        mm._to_js = self._make_to_js_shim()
        mm._js_object_from_entries = self._make_from_entries_shim()
        try:
            mgr1 = mm.MessageManager(
                messages_api_url=messages_api_url,
                config_api_url=config_api_url,
                api_key=api_key,
                is_browser=True,
            )
            for item in seed_messages:
                mgr1._handle_message(item)
            mgr1._handle_config(seed_config)
            mgr1._write_cache()
            # The cache key includes the sign name, which changes
            # during the test (default → "Test Sign"), so the store
            # ends up with two entries: one keyed by the default and
            # one by "Test Sign". The latest write is the one we
            # want to assert against — pick the entry whose key
            # matches the current cache key.
            key = mgr1._cache_key()
            assert key in store, f"expected {key!r} in store, found: {list(store.keys())}"
            written = store[key]
        finally:
            mm._js_session_storage = None
            mm._js_json = None
            mm._to_js = None
            mm._js_object_from_entries = None
        # Confirm shape
        payload = json.loads(written)
        assert payload["v"] == 1
        assert payload["sign_name"] == seed_config["sign"]["name"]
        assert len(payload["messages"]) == 2
        assert payload["config"]["sign"]["name"] == "Test Sign"
        # Second manager: hydrate from the same store. The
        # sign_name check requires mgr2's sign.name to match
        # the cache entry's sign_name, so seed it with the
        # same name. In production, the sign name comes from
        # the cached config — the gate is there so a tab for
        # sign A can never hydrate from a tab for sign B.
        mm = _mm()
        mm._js_session_storage = ss_shim
        mm._js_json = json_shim
        mm._to_js = self._make_to_js_shim()
        mm._js_object_from_entries = self._make_from_entries_shim()
        try:
            mgr2 = mm.MessageManager(
                messages_api_url=messages_api_url,
                config_api_url=config_api_url,
                api_key=api_key,
                is_browser=True,
            )
            mgr2._config.sign = SignSettings(name=seed_config["sign"]["name"])
            hit = asyncio.run(mgr2.hydrate_from_cache())
            assert hit is True
            msgs = mgr2.get_messages(limit=10, suppress=False)
            assert len(msgs) == 2
            assert {m.message.id for m in msgs} == {"m1", "m2"}
            assert mgr2.config.sign.name == "Test Sign"
            assert mgr2.config.timezone == "US/Pacific"
        finally:
            mm._js_session_storage = None
            mm._js_json = None
            mm._to_js = None
            mm._js_object_from_entries = None

    def test_hydrate_from_cache_fires_on_change(self, messages_api_url, config_api_url, api_key):
        """Cache hit fires on_change exactly once."""
        store, ss_shim = self._make_session_storage_shim()
        json_shim = self._make_json_shim()
        store["lindsay50:seed:v1:Lindsay's Heart"] = json.dumps(
            {
                "v": 1,
                "sign_name": "Lindsay's Heart",
                "messages": [
                    {
                        "id": "m1",
                        "sender": "+15551111111",
                        "body": "cached",
                        "received_at": "2026-06-01T10:00:00Z",
                        "source": "rest",
                    }
                ],
                "config": {
                    "filters": [],
                    "senders": [],
                    "effects_settings": {
                        "effects": [{"name": "Hyperspace", "enabled": True}],
                        "fade_seconds": 2.0,
                        "hold_seconds": 15.0,
                        "intro_seconds": 5.0,
                        "idle_seconds": 300.0,
                        "recent_count": 5,
                    },
                    "text_settings": {"speed": 3, "color": 16711680, "text_effect": "scroll"},
                    "sign": {"name": "Lindsay's Heart"},
                    "timezone": "US/Pacific",
                    "version": 2,
                },
            }
        )
        cb = MagicMock()
        mm = _mm()
        mm._js_session_storage = ss_shim
        mm._js_json = json_shim
        mm._to_js = self._make_to_js_shim()
        mm._js_object_from_entries = self._make_from_entries_shim()
        try:
            mgr = mm.MessageManager(
                messages_api_url=messages_api_url,
                config_api_url=config_api_url,
                api_key=api_key,
                is_browser=True,
                on_change=cb,
            )
            hit = asyncio.run(mgr.hydrate_from_cache())
            assert hit is True
            cb.assert_called_once_with()
        finally:
            mm._js_session_storage = None
            mm._js_json = None
            mm._to_js = None
            mm._js_object_from_entries = None

    def test_handle_message_writes_cache(self, messages_api_url, config_api_url, api_key):
        """_handle_message on a browser mgr writes the cache via _emit_change."""
        store, ss_shim = self._make_session_storage_shim()
        json_shim = self._make_json_shim()
        mm = _mm()
        mm._js_session_storage = ss_shim
        mm._js_json = json_shim
        mm._to_js = self._make_to_js_shim()
        mm._js_object_from_entries = self._make_from_entries_shim()
        try:
            mgr = mm.MessageManager(
                messages_api_url=messages_api_url,
                config_api_url=config_api_url,
                api_key=api_key,
                is_browser=True,
            )
            mgr._handle_message(
                {
                    "id": "m1",
                    "sender": "+15551234567",
                    "body": "hi",
                    "received_at": "2026-06-01T12:00:00Z",
                }
            )
            assert len(store) == 1
            payload = json.loads(list(store.values())[0])
            assert any(m["id"] == "m1" for m in payload["messages"])
        finally:
            mm._js_session_storage = None
            mm._js_json = None
            mm._to_js = None
            mm._js_object_from_entries = None

    def test_handle_config_writes_cache(self, messages_api_url, config_api_url, api_key):
        """_handle_config on a browser mgr writes the cache via _emit_change."""
        store, ss_shim = self._make_session_storage_shim()
        json_shim = self._make_json_shim()
        mm = _mm()
        mm._js_session_storage = ss_shim
        mm._js_json = json_shim
        mm._to_js = self._make_to_js_shim()
        mm._js_object_from_entries = self._make_from_entries_shim()
        try:
            mgr = mm.MessageManager(
                messages_api_url=messages_api_url,
                config_api_url=config_api_url,
                api_key=api_key,
                is_browser=True,
            )
            mgr._handle_config(
                {
                    "filters": [],
                    "senders": [],
                    "effects_settings": {
                        "effects": [{"name": "Hyperspace", "enabled": True}],
                        "fade_seconds": 2.0,
                        "hold_seconds": 15.0,
                        "intro_seconds": 5.0,
                        "idle_seconds": 300.0,
                        "recent_count": 5,
                    },
                    "text_settings": {
                        "speed": 3,
                        "color": 16711680,
                        "text_effect": "scroll",
                    },
                    "sign": {"name": "Lindsay's Heart"},
                    "timezone": "US/Pacific",
                    "version": 2,
                }
            )
            assert len(store) == 1
            payload = json.loads(list(store.values())[0])
            assert payload["config"]["timezone"] == "US/Pacific"
        finally:
            mm._js_session_storage = None
            mm._js_json = None
            mm._to_js = None
            mm._js_object_from_entries = None

    def test_seed_writes_cache(self, messages_api_url, config_api_url, api_key, seed_messages, seed_config):
        """seed() writes the cache on completion via the trailing _emit_change."""
        store, ss_shim = self._make_session_storage_shim()
        json_shim = self._make_json_shim()
        mm = _mm()
        mm._js_session_storage = ss_shim
        mm._js_json = json_shim
        mm._to_js = self._make_to_js_shim()
        mm._js_object_from_entries = self._make_from_entries_shim()
        try:
            mgr = mm.MessageManager(
                messages_api_url=messages_api_url,
                config_api_url=config_api_url,
                api_key=api_key,
                is_browser=True,
            )

            async def mock_fetch(url):
                return seed_messages if url == messages_api_url else seed_config

            mgr._fetch = mock_fetch  # type: ignore[assignment]
            asyncio.run(mgr.seed())
            assert len(store) == 1
            payload = json.loads(list(store.values())[0])
            assert len(payload["messages"]) == 2
        finally:
            mm._js_session_storage = None
            mm._js_json = None
            mm._to_js = None
            mm._js_object_from_entries = None

    def test_cache_write_is_noop_on_server_path(self, messages_api_url, config_api_url, api_key):
        """Pi mgr (is_browser=False) does not touch sessionStorage."""
        store, ss_shim = self._make_session_storage_shim()
        json_shim = self._make_json_shim()
        mm = _mm()
        mm._js_session_storage = ss_shim
        mm._js_json = json_shim
        mm._to_js = self._make_to_js_shim()
        mm._js_object_from_entries = self._make_from_entries_shim()
        try:
            mgr = mm.MessageManager(
                messages_api_url=messages_api_url,
                config_api_url=config_api_url,
                api_key=api_key,
                is_browser=False,
            )
            mgr._handle_message(
                {
                    "id": "m1",
                    "sender": "+15551234567",
                    "body": "hi",
                    "received_at": "2026-06-01T12:00:00Z",
                }
            )
            # Server path never touches sessionStorage
            assert len(store) == 0
            assert mgr._cache_key() == ""
        finally:
            mm._js_session_storage = None
            mm._js_json = None
            mm._to_js = None
            mm._js_object_from_entries = None

    def test_cache_write_exception_is_swallowed(self, messages_api_url, config_api_url, api_key):
        """sessionStorage raising doesn't break the buffer write."""
        ss_shim = MagicMock()
        ss_shim.setItem.side_effect = RuntimeError("quota exceeded")
        json_shim = self._make_json_shim()
        mm = _mm()
        mm._js_session_storage = ss_shim
        mm._js_json = json_shim
        mm._to_js = self._make_to_js_shim()
        mm._js_object_from_entries = self._make_from_entries_shim()
        try:
            mgr = mm.MessageManager(
                messages_api_url=messages_api_url,
                config_api_url=config_api_url,
                api_key=api_key,
                is_browser=True,
            )
            # Should not raise
            mgr._handle_message(
                {
                    "id": "m1",
                    "sender": "+15551234567",
                    "body": "hi",
                    "received_at": "2026-06-01T12:00:00Z",
                }
            )
            # Buffer mutation succeeded despite the cache failure
            msgs = mgr.get_messages(limit=10, suppress=False)
            assert len(msgs) == 1
            assert msgs[0].message.id == "m1"
        finally:
            mm._js_session_storage = None
            mm._js_json = None
            mm._to_js = None
            mm._js_object_from_entries = None

    def test_seed_clears_cache_before_fetch(
        self, messages_api_url, config_api_url, api_key, seed_messages, seed_config
    ):
        """seed() wipes the cache before fetching and rewrites it after."""
        store, ss_shim = self._make_session_storage_shim()
        json_shim = self._make_json_shim()
        # Pre-populate the cache with an entry that would otherwise
        # hydrate. The seed must wipe it before the fetch.
        store["lindsay50:seed:v1:Lindsay's Heart"] = json.dumps(
            {
                "v": 1,
                "sign_name": "Lindsay's Heart",
                "messages": [
                    {
                        "id": "stale",
                        "sender": "+15550000000",
                        "body": "should be wiped",
                        "received_at": "2026-06-01T09:00:00Z",
                        "source": "rest",
                    }
                ],
                "config": {"sign": {"name": "Lindsay's Heart"}, "version": 2},
            }
        )
        mm = _mm()
        mm._js_session_storage = ss_shim
        mm._js_json = json_shim
        mm._to_js = self._make_to_js_shim()
        mm._js_object_from_entries = self._make_from_entries_shim()
        try:
            mgr = mm.MessageManager(
                messages_api_url=messages_api_url,
                config_api_url=config_api_url,
                api_key=api_key,
                is_browser=True,
            )

            async def mock_fetch(url):
                # The cache should be empty at this point — the seed
                # cleared it before calling _fetch.
                assert len(store) == 0, f"cache not cleared before fetch: {list(store.keys())}"
                return seed_messages if url == messages_api_url else seed_config

            mgr._fetch = mock_fetch  # type: ignore[assignment]
            asyncio.run(mgr.seed())
            # After seed, the cache is rewritten with the fetched data
            assert len(store) == 1
            payload = json.loads(list(store.values())[0])
            assert all(m["id"] != "stale" for m in payload["messages"])
        finally:
            mm._js_session_storage = None
            mm._js_json = None
            mm._to_js = None
            mm._js_object_from_entries = None


class TestRingBufferEviction:
    def test_101st_message_evicts_oldest(self, messages_api_url, config_api_url, api_key):
        """Adding a 101st message evicts the oldest (smallest received_at)."""
        mm = _mm()
        mgr = mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
        )
        # Add 100 messages
        for i in range(100):
            mgr.dispatch(
                _make_env(
                    {
                        "id": f"m{i:03d}",
                        "sender": "+1",
                        "body": f"msg {i}",
                        "received_at": f"2026-06-01T{10 + i // 60:02d}:{i % 60:02d}:00Z",
                    }
                )
            )
        assert len(mgr.get_messages(limit=200, suppress=False)) == 100
        # Add a 101st message (newer than all)
        mgr.dispatch(
            _make_env(
                {
                    "id": "m100",
                    "sender": "+1",
                    "body": "newest",
                    "received_at": "2026-06-01T15:00:00Z",
                }
            )
        )
        all_msgs = mgr.get_messages(limit=200, suppress=False)
        assert len(all_msgs) == 100
        # Newest first
        assert all_msgs[0].message.id == "m100"
        # The oldest (m000) was evicted
        ids = {m.message.id for m in all_msgs}
        assert "m000" not in ids
        assert "m100" in ids


# ---------------------------------------------------------------------------
# AST guard: no top-level `import requests` or `import js` / `from js`
# ---------------------------------------------------------------------------


class TestTopLevelImportsGuard:
    def _get_top_level_imports(self):
        src = MM_PATH.read_text()
        tree = ast.parse(src)
        top_level = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top_level.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top_level.append(node.module)
        return top_level

    def test_no_top_level_requests_import(self):
        """No `import requests` at module top (lazy-loaded inside _fetch)."""
        top = self._get_top_level_imports()
        assert "requests" not in top, f"requests must be lazy: top-level imports: {top}"

    def test_no_top_level_js_import(self):
        """No `import js` or `from js import ...` at module top (lazy-loaded inside _fetch)."""
        top = self._get_top_level_imports()
        assert "js" not in top, f"js must be lazy: top-level imports: {top}"
        for name in top:
            assert not name.startswith("js."), f"`from js import ...` must be lazy: top-level imports: {top}"

    def test_no_top_level_config_reader_import(self):
        """No `from lib_shared.config_reader import get_config` at module top."""
        top = self._get_top_level_imports()
        assert (
            "lib_shared.config_reader" not in top
        ), f"config_reader must not be imported at top: top-level imports: {top}"


# ---------------------------------------------------------------------------
# Wire-strip on override (effects-override spec: D7)
# ---------------------------------------------------------------------------


class TestHandleConfigOverrideStrip:
    """When the operator has an active `effects_settings` override on the
    Pi, the wire's `effects_settings` block is dropped before the in-memory
    config sees it. Top-level `text_settings`, `filters`, `senders`,
    `sign`, and `timezone` still come from the wire."""

    @pytest.fixture
    def manager(self, messages_api_url, config_api_url, api_key):
        mm = _mm()
        return mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
        )

    @pytest.fixture
    def override_active(self, monkeypatch):
        """Make `is_effects_settings_override_active()` return True."""
        from lib_shared import effects_loader

        monkeypatch.setattr(effects_loader, "is_effects_settings_override_active", lambda: True)

    @pytest.fixture
    def override_inactive(self, monkeypatch):
        """Make `is_effects_settings_override_active()` return False."""
        from lib_shared import effects_loader

        monkeypatch.setattr(effects_loader, "is_effects_settings_override_active", lambda: False)

    def test_override_active_strips_effects_settings_block(self, manager, override_active):
        """Wire `effects_settings` is dropped when override is active."""
        manager._handle_config(
            {
                "text_settings": {"speed": 4, "color": 0x00FF00, "text_effect": "scroll"},
                "effects_settings": {
                    "effects": [{"name": "Fireworks", "enabled": True}],
                    "fade_seconds": 9.0,
                    "hold_seconds": 9.0,
                    "intro_seconds": 9.0,
                    "idle_seconds": 9.0,
                    "recent_count": 9,
                },
                "filters": [{"type": "keyword", "pattern": "spam", "action": "suppress"}],
            }
        )
        # effects_settings from the wire did NOT land — the loader-driven
        # canonical value (recent_count=5) is what the manager holds.
        assert manager.config.effects_settings.recent_count == 5
        assert manager.config.effects_settings.fade_seconds == 2.0
        # But text_settings and filters DID land from the wire.
        assert manager.config.text_settings.speed == 4
        assert manager.config.text_settings.color == 0x00FF00
        assert len(manager.config.filters) == 1
        assert manager.config.filters[0].pattern == "spam"

    def test_override_inactive_preserves_effects_settings_block(self, manager, override_inactive):
        """Wire `effects_settings` is applied when no override is active."""
        manager._handle_config(
            {
                "effects_settings": {
                    "effects": [{"name": "Fireworks", "enabled": True}],
                    "fade_seconds": 7.0,
                    "hold_seconds": 7.0,
                    "intro_seconds": 7.0,
                    "idle_seconds": 7.0,
                    "recent_count": 7,
                },
            }
        )
        assert manager.config.effects_settings.recent_count == 7
        assert manager.config.effects_settings.fade_seconds == 7.0

    def test_override_active_text_only_passes_through(self, manager, override_active):
        """Override active + wire sends only text_settings → text applies."""
        manager._handle_config(
            {
                "text_settings": {"speed": 2, "color": 0xABCDEF, "text_effect": "scroll"},
            }
        )
        assert manager.config.text_settings.speed == 2
        assert manager.config.text_settings.color == 0xABCDEF

    def test_override_active_timezone_and_filters_pass_through(self, manager, override_active):
        """Override active: timezone and filters come from the wire."""
        manager._handle_config(
            {
                "timezone": "US/Eastern",
                "filters": [{"type": "sender", "pattern": "+15550000000", "action": "suppress"}],
            }
        )
        assert manager.config.timezone == "US/Eastern"
        assert len(manager.config.filters) == 1
        assert manager.config.filters[0].type == "sender"
