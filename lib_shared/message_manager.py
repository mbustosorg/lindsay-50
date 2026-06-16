"""MessageManager — owns config + message storage, handles dispatch and seeding.

Both the Raspberry Pi display and the browser's PyScript runtime instantiate
this. URLs and credentials are injected as constructor parameters so the
class is config-agnostic; the `is_browser` flag selects between two I/O
implementations (server: `requests` for the device; browser: `js.fetch` for
PyScript). Both paths are lazy-imported inside the seed-fetch helper so the
module is importable in either runtime without a top-level network dependency.
"""

import asyncio
import json
import logging
from typing import Callable, Optional

from lib_shared.models import MessageEnvelope, Message, SignConfig
from lib_shared.messages import InMemoryMessages

logger = logging.getLogger(__name__)


def _ensure_browser_runtime():
    """Lazily import `js.fetch` — only available inside a browser runtime (Pyodide)."""
    global _js_fetch
    if _js_fetch is None:
        from js import fetch as _js_fetch  # type: ignore[import-not-found]  # noqa: F811
    return _js_fetch


def _ensure_server_runtime():
    """Lazily import `requests` — only available in a standard Python runtime."""
    global _requests
    if _requests is None:
        import requests as _requests  # noqa: F811
    return _requests


_js_fetch = None
_js_object_from_entries = None
_to_js = None
_requests = None


def _ensure_js_object_from_entries():
    """Lazily import `js.Object.fromEntries` — only available inside a browser runtime.

    `RequestInit.headers` is stricter than a destructure-params
    case: a bare Python dict crossing the Pyodide boundary becomes
    a JsProxy, and `fetch` rejects it with
    "Failed to read the 'headers' property from 'RequestInit':
    The provided value cannot be converted to a sequence". We
    need a real JS object — we build one via
    `Object.fromEntries([[k, v], ...])`.
    """
    global _js_object_from_entries
    if _js_object_from_entries is None:
        from js import Object as _js_object  # type: ignore[import-not-found]

        _js_object_from_entries = _js_object.fromEntries
    return _js_object_from_entries


def _ensure_to_js():
    """Lazily import `pyodide.ffi.to_js` — only available inside a browser runtime.

    Used to convert Python `[[k, v], ...]` lists to JS arrays of
    [k, v] pairs that `Object.fromEntries` can consume. Kept
    lazy so the module is importable in non-Pyodide runtimes
    (server-side tests, the device).
    """
    global _to_js
    if _to_js is None:
        from pyodide.ffi import to_js as _to_js  # type: ignore[import-not-found]
    return _to_js


class MessageManager:
    """Owns SignConfig + InMemoryMessages; handles dispatch, seeding, and storage.

    On the Raspberry Pi: constructed with `is_browser=False`; the seed fetch
    uses `requests` in a worker thread. On the browser (PyScript): constructed
    with `is_browser=True`; the seed fetch uses `js.fetch` with an `X-API-Key`
    header (the same value the device uses). The dispatch / ring-buffer /
    filter / config-update logic is identical in both environments.
    """

    def __init__(
        self,
        messages_api_url: str,
        config_api_url: str,
        api_key: str,
        is_browser: bool = False,
        on_message: Optional[Callable[[Message], None]] = None,
    ) -> None:
        """Create MessageManager with explicit URLs and an `is_browser` flag.

        Args:
            messages_api_url: URL of the messages REST endpoint (e.g. /api/messages).
            config_api_url:   URL of the config REST endpoint (e.g. /api/config).
            api_key:          the X-API-Key the device uses; the same value is
                              used for the seed fetch in both environments.
            is_browser:       True when running in the browser (PyScript / Pyodide);
                              defaults to False (the device path). The device's
                              call site does not pass this kwarg; the browser's
                              call site always passes `is_browser=True` as a
                              hardcoded literal in PyScript.
            on_message:       callback(msg: Message) — invoked when a "message"
                              envelope arrives over MQTT. The Pi uses this to
                              trigger display updates.
        """
        self._config = SignConfig()
        self._messages = InMemoryMessages(self._config, maxlen=100)
        self._on_message = on_message
        self._messages_api_url = messages_api_url
        self._config_api_url = config_api_url
        self._api_key = api_key
        self._is_browser = is_browser

    @property
    def config(self) -> SignConfig:
        return self._config

    @property
    def messages(self) -> InMemoryMessages:
        return self._messages

    def dispatch(self, raw: str) -> None:
        """Parse MessageEnvelope from raw MQTT payload, update internal state."""
        try:
            envelope = MessageEnvelope.from_json(raw)
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("Invalid MessageEnvelope: %s", e)
            return

        if envelope.type == "message":
            self._handle_message(envelope.payload)
        elif envelope.type == "config":
            self._handle_config(envelope.payload)
        else:
            logger.warning("Unknown envelope type: %r", envelope.type)

    def _handle_message(self, payload: dict) -> None:
        """Convert payload dict to Message, store it, and call _on_message callback.

        Filter rules are evaluated AFTER the message is added to the ring
        buffer (so the suppressed state is recorded) but BEFORE the
        on_message callback fires — filtered messages do not trigger
        on_message per the spec.
        """
        msg = Message(
            id=payload.get("id", ""),
            sender=payload.get("sender", ""),
            body=payload.get("body", ""),
            received_at=payload.get("received_at", ""),
        )

        self._messages.add(msg, source="mqtt")
        logger.info("MessageManager routed message id=%s body=%r", msg.id, msg.body[:40])
        # Only fire on_message for non-suppressed messages. The filter
        # evaluation reuses the same `_apply_filter` the InMemoryMessages
        # enrichment path uses, so the suppressed flag is consistent.
        if self._on_message:
            suppressing = self._messages._apply_filter(msg, self._config.filters)
            if not suppressing:
                self._on_message(msg)

    def _handle_config(self, payload: dict) -> None:
        """Apply a SignConfig dict to the in-memory config (thread-safe update)."""
        self._config.update_from_dict(payload)
        logger.info("MessageManager applied config update")

    async def _fetch(self, url: str) -> dict:
        """One HTTP GET to a JSON endpoint, returning the parsed dict.

        Server: requests.get in a worker thread (sync lib, async-friendly).
        Browser: js.fetch with the X-API-Key header (already async).
        """
        if self._is_browser:
            js_fetch = _ensure_browser_runtime()
            # Pyodide 0.26's `RequestInit.headers` is stricter than
            # the destructure-params case we hit in the WS shim — a
            # bare Python dict crossing the boundary becomes a
            # JsProxy, and `fetch` rejects it with
            # "Failed to read the 'headers' property from 'RequestInit':
            # The provided value cannot be converted to a sequence"
            # (the live symptom was a `MessageManager message seed
            # failed: TypeError: ...` log every page load, with the
            # in-memory ring buffer permanently empty). Build a real
            # JS object via `Object.fromEntries([[k, v], ...])` and
            # convert the `[[k, v]]` Python list to a JS array via
            # `to_js` so `fetch` sees a plain record it can convert
            # to a Headers instance.
            js_from_entries = _ensure_js_object_from_entries()
            to_js = _ensure_to_js()

            def _call_fetch():
                return js_fetch(
                    url,
                    method="GET",
                    headers=js_from_entries(to_js([["X-API-Key", self._api_key]])),
                )

            response = await _call_fetch()
            if not response.ok:
                raise RuntimeError(f"seed fetch {url} returned HTTP {response.status}")

            def _call_json():
                return response.json()

            # `response.json()` resolves to a Pyodide JsProxy of a
            # JS object/array, not a real Python dict. The messages
            # seed path happens to cope (it iterates as a list and
            # calls .get on dict-like proxies), but the config seed
            # path passes the result directly to
            # `SignConfig.update_from_dict`, which calls `dict(...)`
            # on it — and a JsProxy of a plain object doesn't
            # implement `keys()` the way Python's dict-ctor expects,
            # so it raises
            # `MessageManager config seed failed: get`
            # (the bare `.get` method name is what `dict.__init__`
            # tries first, before falling back to iter). Convert to
            # a real Python object via `.to_py()` so downstream
            # code can treat it as a plain dict/list.
            raw = await _call_json()
            if hasattr(raw, "to_py"):
                return raw.to_py()
            return raw
        else:
            requests = _ensure_server_runtime()

            def _sync_get():
                r = requests.get(url, headers={"X-API-Key": self._api_key}, timeout=5)
                r.raise_for_status()
                return r.json()

            return await asyncio.to_thread(_sync_get)

    async def seed(self) -> None:
        """Back-populate config and messages from the Flask REST API.

        Uses the internal `_fetch` helper for both endpoints, so the same
        X-API-Key auth path runs in both the device and the browser.
        """
        if self._messages_api_url:
            try:
                data = await self._fetch(self._messages_api_url)
                print(f"[seed] _fetch returned type={type(data).__name__} "
                      f"isinstance(list)={isinstance(data, list)} "
                      f"len={len(data) if isinstance(data, list) else 'N/A'}",
                      flush=True)
                if isinstance(data, list):
                    self._messages.clear()
                    msgs = [
                        Message(
                            id=item.get("id", ""),
                            sender=item.get("sender", ""),
                            body=item.get("body", ""),
                            received_at=item.get("received_at", ""),
                        )
                        for item in data[-100:]
                    ]
                    self._messages.add_many(msgs, source="rest")
                logger.info(
                    "MessageManager seeded %d messages",
                    len(data) if isinstance(data, list) else 0,
                )
            except Exception as e:
                print(f"[seed] EXCEPTION: {e!r}", flush=True)
                logger.warning("MessageManager message seed failed: %s", e)

        if self._config_api_url:
            try:
                cfg_dict = await self._fetch(self._config_api_url)
                self._config.update_from_dict(cfg_dict)
                logger.info("MessageManager seeded config")
            except Exception as e:
                logger.warning("MessageManager config seed failed: %s", e)

    def get_messages(self, limit: int = 100, suppress: bool = True):
        """Return messages from the ring buffer.

        Args:
            limit: Maximum number of messages to return.
            suppress: If True (default), exclude suppressed messages.
        """
        return self._messages.get_messages(limit, suppress=suppress)

    def get_config(self) -> SignConfig:
        return self._config
