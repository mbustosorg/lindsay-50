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
from lib_shared.models import FilterRule, SignConfig  # noqa: E402

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
        "effect_settings": {
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
        """Constructor accepts (messages_api_url, config_api_url, api_key, is_browser, on_message)."""
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
        assert mgr._on_message is None
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

    def test_constructor_with_on_message(self, messages_api_url, config_api_url, api_key):
        """on_message callback is stored."""
        cb = MagicMock()
        mm = _mm()
        mgr = mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_message=cb,
        )
        assert mgr._on_message is cb


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
# dispatch — message and config envelopes, filter rules, on_message callback
# ---------------------------------------------------------------------------


class TestDispatchMessage:
    def test_dispatch_message_envelope_routes_to_ring(self, messages_api_url, config_api_url, api_key):
        """A type=message envelope is added to the ring buffer and on_message is invoked."""
        cb = MagicMock()
        mm = _mm()
        mgr = mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_message=cb,
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
        cb.assert_called_once()
        # The callback received the Message object
        cb_arg = cb.call_args[0][0]
        assert cb_arg.id == "x1"
        assert cb_arg.body == "hi"

    def test_dispatch_does_not_invoke_on_message_on_config(self, messages_api_url, config_api_url, api_key):
        """A type=config envelope updates the config but does NOT invoke on_message."""
        cb = MagicMock()
        mm = _mm()
        mgr = mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_message=cb,
        )
        env = _make_config_env(
            {
                "filters": [],
                "senders": [],
                "effect_settings": {
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
        names = [e["name"] for e in mgr.config.effect_settings.effects]
        assert "Fireworks" in names
        # on_message NOT called
        cb.assert_not_called()

    def test_dispatch_malformed_envelope_is_dropped(self, messages_api_url, config_api_url, api_key):
        """A malformed envelope is dropped without raising."""
        cb = MagicMock()
        mm = _mm()
        mgr = mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_message=cb,
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
# Filter rules
# ---------------------------------------------------------------------------


class TestDispatchFilterRules:
    def test_filtered_message_does_not_invoke_on_message(self, messages_api_url, config_api_url, api_key):
        """A message matching a filter rule is added but on_message is NOT invoked."""
        cb = MagicMock()
        mm = _mm()
        mgr = mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_message=cb,
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
        # but on_message was not called
        cb.assert_not_called()

    def test_non_filtered_message_invokes_on_message(self, messages_api_url, config_api_url, api_key):
        """A non-matching message invokes on_message and is not suppressed."""
        cb = MagicMock()
        mm = _mm()
        mgr = mm.MessageManager(
            messages_api_url=messages_api_url,
            config_api_url=config_api_url,
            api_key=api_key,
            on_message=cb,
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
