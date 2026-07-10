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

from lib_shared.models import EffectsSettings, MessageEnvelope, Message, SignConfig, TextSettings
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
_js_session_storage = None
_js_json = None


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


def _ensure_js_session_storage():
    """Lazily import `js.sessionStorage` — only available in a browser runtime.

    Used to persist the post-seed state across full-page
    navigations within a session so the testing/preview pages
    re-render from cache instead of waiting for a network
    re-seed on every nav. Module-level (lazy-resolved) so
    tests can patch it with a `MagicMock` shim.
    """
    global _js_session_storage
    if _js_session_storage is None:
        from js import sessionStorage as _js_session_storage  # type: ignore[import-not-found]
    return _js_session_storage


def _ensure_js_json():
    """Lazily import `js.JSON` — only available in a browser runtime.

    The browser's `JSON.stringify` / `JSON.parse` round-trips
    any JS-encodable value (including `Date`s and `undefined`),
    which is safer than `json.dumps` for a `SignConfig` that
    might have a model-incompatible nested shape after a
    future schema change. Same lazy-import + module-level
    pattern as `_ensure_js_session_storage` for testability.
    """
    global _js_json
    if _js_json is None:
        from js import JSON as _js_json  # type: ignore[import-not-found]
    return _js_json


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
        on_change: Optional[Callable[[], None]] = None,
        on_check_for_update: Optional[Callable[[], None]] = None,
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
            on_change:        parameterless callback invoked after any state
                              mutation (message added, config updated, seed
                              completed). The Pi's rAF loop and the browser's
                              per-page `reRender` both rely on this. Exceptions
                              from the callback are swallowed so a faulty
                              listener never breaks the buffer write.
            on_check_for_update: Optional callback invoked when a
                              `type=command, action=check-for-update`
                              envelope arrives. The Pi's controller wires
                              this to its loader-entrypoint. The browser
                              leaves it None. Exceptions are logged and
                              swallowed so a faulty callback never
                              interrupts the paho network thread.
        """
        self._config = SignConfig()
        self._messages = InMemoryMessages(self._config, maxlen=100)
        self._on_change = on_change
        self._on_check_for_update = on_check_for_update
        self._messages_api_url = messages_api_url
        self._config_api_url = config_api_url
        self._api_key = api_key
        self._is_browser = is_browser

    def _emit_change(self) -> None:
        """Fire the `on_change` callback if registered, then persist to sessionStorage.

        Exceptions from the callback are swallowed — a faulty
        listener must never break the buffer write. The cache
        write runs AFTER the callback so a listener that
        throws can't suppress persistence. The cache write
        itself is also exception-swallowed (private mode,
        quota exceeded) for the same reason.

        The cache is browser-only; on the Pi this is a no-op.
        The WS keeps the cache live across full-page
        navigations within a session.
        """
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception as e:
                logger.warning("MessageManager on_change callback raised: %s", e)
        if self._is_browser:
            self._write_cache()

    # --- sessionStorage cache (browser-only) ---

    _CACHE_VERSION = 1
    _CACHE_KEY_PREFIX = "lindsay50:seed:v1:"

    def _cache_key(self) -> str:
        """Return the per-sign sessionStorage key for the seed cache.

        The sign_name is taken from the in-memory config so a
        tab for sign A can never hydrate from a tab for sign
        B's cache. The `v1` prefix lets us invalidate the
        cache when the on-disk format changes.
        """
        if not self._is_browser:
            return ""
        sign_name = "unknown"
        try:
            sign = getattr(self._config, "sign", None)
            if sign is not None:
                sign_name = getattr(sign, "name", None) or "unknown"
        except Exception:
            pass
        return f"{self._CACHE_KEY_PREFIX}{sign_name}"

    def _write_cache(self) -> None:
        """Persist current state to sessionStorage. Browser-only. Swallows errors."""
        key = self._cache_key()
        if not key:
            return
        try:
            payload = {
                "v": self._CACHE_VERSION,
                "sign_name": self._config.sign.name if self._config.sign else "unknown",
                "messages": [m.to_dict() for m in self._messages._msgs],
                "config": self._config.to_dict(),
            }
            print(
                f"[mm-cache] CACHE_WRITE key={key} buffer_size={len(payload['messages'])} "
                f"media_total={sum(len(m.get('media') or []) for m in payload['messages'])} "
                f"sample_media={next((m.get('media') for m in payload['messages'] if m.get('media')), None)!r}",
                flush=True,
            )
            # `payload` is a Python dict with nested dicts (the
            # `config` value comes from `SignConfig.to_dict()`,
            # which returns a dict-of-dicts of effects / text
            # settings). Passing it directly to `JSON.stringify`
            # lets Pyodide auto-convert to a JsProxy, but
            # `JSON.stringify(JsProxy)` on a *Python* dict with
            # nested *Python* dicts silently emits just `"{}"` —
            # the inner proxies can't be walked by the JS
            # stringifier, so all nested keys are dropped. The
            # live symptom was sessionStorage holding `"{}"`
            # instead of the actual cache (preview='{}' from
            # `_write_cache`, then `hydrate_from_cache` parses
            # to an empty dict and rejects on version mismatch).
            # Convert with `to_js` first so the nested objects
            # become real JS objects the stringifier can walk.
            to_js = _ensure_to_js()
            ss = _ensure_js_session_storage()
            j = _ensure_js_json()
            payload_js = to_js(payload, dict_converter=_ensure_js_object_from_entries())
            serialized = j.stringify(payload_js)
            ss.setItem(key, serialized)
        except Exception as e:
            logger.warning("MessageManager cache write failed: %s", e)

    def _clear_cache(self) -> None:
        """Remove the sessionStorage cache entry. Browser-only. Swallows errors."""
        key = self._cache_key()
        if not key:
            return
        try:
            ss = _ensure_js_session_storage()
            ss.removeItem(key)
        except Exception as e:
            logger.warning("MessageManager cache clear failed: %s", e)

    async def hydrate_from_cache(self) -> bool:
        """Populate state from sessionStorage if a valid entry exists.

        Returns True on a successful hit (and fires `on_change`
        once so per-page listeners re-render with the cached
        state). Returns False on miss / version mismatch /
        sign mismatch / corruption / any per-message missing
        field — those are all "treat as no cache" cases and
        do NOT fire `on_change`. The caller falls back to a
        network seed on False.

        Browser-only. The Pi returns False immediately.

        The per-message `source` field is required — it's
        how /testing distinguishes live WS envelopes from
        the initial REST backfill, and silently defaulting
        to "rest" made every hydrated message look like a
        backfill. A missing field on any item rejects the
        whole cache so the page re-seeds cleanly.
        """
        if not self._is_browser:
            return False
        key = self._cache_key()
        if not key:
            return False
        try:
            ss = _ensure_js_session_storage()
            j = _ensure_js_json()
            raw = ss.getItem(key)
        except Exception as e:
            logger.warning("MessageManager cache read failed: %s", e)
            return False
        if not raw:
            return False
        try:
            payload = j.parse(raw)
            # Pyodide JsProxy → Python dict
            if hasattr(payload, "to_py"):
                payload = payload.to_py()
        except Exception as e:
            logger.warning("MessageManager cache parse failed: %s", e)
            return False
        if not isinstance(payload, dict):
            return False
        if payload.get("v") != self._CACHE_VERSION:
            return False
        expected_sign = self._config.sign.name if self._config.sign else "unknown"
        if payload.get("sign_name") != expected_sign:
            return False
        msgs_raw = payload.get("messages") or []
        cfg_raw = payload.get("config")
        if not isinstance(msgs_raw, list) or not isinstance(cfg_raw, dict):
            return False
        try:
            self._messages.clear()
            # Per-message hydrate. Each item must carry a
            # source — anything missing falls through to the
            # re-seed path below. `add_many` is single-source
            # for a batch, so we use `add` to preserve the
            # mix of rest + mqtt messages a live cache holds.
            for item in msgs_raw:
                if not isinstance(item, dict):
                    return False
                src = item.get("source")
                if src not in ("rest", "mqtt"):
                    return False
                self._messages.add(
                    Message(
                        id=item.get("id", ""),
                        sender=item.get("sender", ""),
                        body=item.get("body", ""),
                        received_at=item.get("received_at", ""),
                        media=item.get("media") or [],
                    ),
                    source=src,
                )
            self._config.update_from_dict(cfg_raw)
            # Hydrate enriches the whole buffer in one pass — the
            # hydrate is rare and a per-message enrich would be O(n²)
            # for no benefit.
            self._messages._enrich_messages(list(self._messages._msgs))
        except Exception as e:
            logger.warning("MessageManager cache hydrate failed: %s", e)
            return False
        print(
            f"[mm-cache] CACHE_HYDRATE key={key} buffer_size={len(self._messages._msgs)} "
            f"media_total={sum(len(getattr(m, 'media', []) or []) for m in self._messages._msgs)} "
            f"sample_media={next((getattr(m, 'media', None) for m in self._messages._msgs if getattr(m, 'media', None)), None)!r}",
            flush=True,
        )
        self._emit_change()
        return True

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
            payload = envelope.payload
            if isinstance(payload, dict):
                logger.info(
                    "[debug-dispatch] ENVELOPE_RECEIVED type=message id=%s body=%r",
                    payload.get("id", ""),
                    (payload.get("body", "") or "")[:40],
                )
            self._handle_message(payload)
        elif envelope.type == "config":
            payload = envelope.payload
            if isinstance(payload, dict):
                filters = payload.get("filters") or []
                logger.info(
                    "[debug-dispatch] ENVELOPE_RECEIVED type=config filter_count=%d filter_ids=%s",
                    len(filters),
                    [f.get("pattern", "") for f in filters if isinstance(f, dict) and f.get("type") == "message"],
                )
            self._handle_config(payload)
        elif envelope.type == "command":
            self._handle_command(envelope.payload)
        else:
            logger.warning("Unknown envelope type: %r", envelope.type)

    def _handle_command(self, payload) -> None:
        """Handle a type=command envelope by dispatching on the `action` field.

        v2 supports exactly one action: `check-for-update` (Flask publishes
        a one-shot hint at startup; the running app compares its
        `LINDSAY50_ACTIVE_SHA` env var to Flask's expected SHA and
        `os.execvpe`s into the loader on mismatch). The handler is the
        `on_check_for_update` callable wired at construction time — a
        simple switch, not a registry, because we have one command.

        Malformed payloads (None, non-dict, missing or non-string `action`)
        are logged and dropped: a buggy publisher must never brick the
        device's render loop. Unknown actions are also logged and dropped
        — adding a new command is a one-line `elif` plus a constructor
        kwarg, not a registry mutation.

        Handler exceptions are caught and logged — a faulty handler is a
        deployment bug, not a render-loop bug, and raising here would
        interrupt the paho network thread.
        """
        if not isinstance(payload, dict):
            logger.warning("MessageManager dropped command: payload is not a dict: %r", payload)
            return
        action = payload.get("action")
        if not isinstance(action, str) or not action:
            logger.warning("MessageManager dropped command: missing or invalid 'action': %r", payload)
            return
        if action == "check-for-update":
            callback = self._on_check_for_update
            if callback is None:
                logger.warning("MessageManager dropped check-for-update: no handler registered")
                return
            try:
                callback()
            except Exception as exc:
                logger.exception("MessageManager on_check_for_update raised: %s", exc)
            return
        logger.warning("MessageManager dropped unknown command action: %r", action)

    def _handle_message(self, payload: dict) -> None:
        """Convert payload dict to Message, store it, enrich it, and emit change.

        The buffer's `add` does its own duplicate-suppression
        (silently drops re-deliveries; returns `None` on dup).
        Enrichment of the new view runs here at event time so the
        next read sees up-to-date derived fields without paying the
        filter / formatter cost on the read path.

        `media` is read off the wire envelope so MMS attachments
        (issue #38) round-trip to the in-memory buffer + the
        coordinator's `BrowserMediaOverlay` / `MediaCycler`. An
        empty list on the wire (the SMS-only case) maps to
        `media=[]` via the `Message` dataclass default; an absent
        key behaves the same. The publish side (`Message.to_dict`)
        always emits the field, so consumers can rely on it being
        present.
        """
        msg = Message(
            id=payload.get("id", ""),
            sender=payload.get("sender", ""),
            body=payload.get("body", ""),
            received_at=payload.get("received_at", ""),
            media=payload.get("media") or [],
        )

        view = self._messages.add(msg, source="mqtt")
        if view is not None:
            self._messages._enrich_messages([view])
        logger.info("MessageManager routed message id=%s body=%r", msg.id, msg.body[:40])
        self._emit_change()

    def _handle_config(self, payload: dict) -> None:
        """Apply a SignConfig dict to the in-memory config and re-enrich buffered messages.

        Filter rules and timezone changes can reclassify previously-stored
        entries (e.g. a message that wasn't suppressed before now matches a
        new rule). Re-enrich on the event that changes the inputs so the
        next `get_messages()` read returns up-to-date values without paying
        the filter / formatter cost on the read path.

        When the operator has an active `effects_settings` override on the
        Pi (env var or `config_overrides/effects_settings.json`), the wire's
        `effects_settings` block is dropped before `update_from_dict` runs.
        The override owns ALL of `EffectsSettings` — both the effects list
        AND the pacing fields — so the wire's block is silently discarded.
        Top-level `text_settings`, `filters`, `senders`, `sign`, and
        `timezone` still come from the wire as normal.
        """
        from lib_shared.effects_loader import is_effects_settings_override_active

        payload = dict(payload) if isinstance(payload, dict) else payload
        if isinstance(payload, dict) and is_effects_settings_override_active() and "effects_settings" in payload:
            logger.debug("MessageManager dropping wire effects_settings (override active)")
            payload.pop("effects_settings", None)
        self._config.update_from_dict(payload or {})
        self._messages._enrich_messages(list(self._messages._msgs))
        post_filters = list(self._config.filters)
        post_suppressed = sum(1 for m in self._messages._msgs if getattr(m, "suppressed", False))
        logger.info(
            "[debug-dispatch] HANDLE_CONFIG_DONE filter_count=%d filter_ids=%s suppressed_in_buffer=%d buffer_size=%d",
            len(post_filters),
            [f.pattern for f in post_filters if f.type == "message"],
            post_suppressed,
            len(self._messages._msgs),
        )
        self._emit_change()

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

        Semantically "refresh from the network, ignoring any
        prior state": the in-memory buffer is cleared and (on
        the browser) the sessionStorage cache is cleared
        before the fetch starts. The trailing `_emit_change()`
        writes the new cache as a side effect, so callers
        don't need to do anything else.

        Uses the internal `_fetch` helper for both endpoints,
        so the same X-API-Key auth path runs in both the
        device and the browser. Emits `on_change` once at the
        end so listeners see the post-seed state in a single
        event (not one per endpoint).
        """
        if self._is_browser:
            self._clear_cache()
        if self._messages_api_url:
            try:
                data = await self._fetch(self._messages_api_url)
                if isinstance(data, list):
                    self._messages.clear()
                    msgs = [
                        Message(
                            id=item.get("id", ""),
                            sender=item.get("sender", ""),
                            body=item.get("body", ""),
                            received_at=item.get("received_at", ""),
                            media=item.get("media") or [],
                        )
                        for item in data[:100]
                    ]
                    self._messages.add_many(msgs, source="rest")
                logger.info(
                    "MessageManager seeded %d messages",
                    len(data) if isinstance(data, list) else 0,
                )
            except Exception as e:
                logger.warning("MessageManager message seed failed: %s", e)

        if self._config_api_url:
            try:
                cfg_dict = await self._fetch(self._config_api_url)
                self._config.update_from_dict(cfg_dict)
                logger.info("MessageManager seeded config")
            except Exception as e:
                logger.warning("MessageManager config seed failed: %s", e)

        # Seed populates messages first, then config — enrich once at
        # the end so the derived fields reflect the final config. The
        # buffer is empty if both fetches failed, so this is a cheap
        # no-op in that case.
        self._messages._enrich_messages(list(self._messages._msgs))
        self._emit_change()

    def get_messages(self, limit: int = 100, suppress: bool = True):
        """Return messages from the ring buffer.

        Args:
            limit: Maximum number of messages to return.
            suppress: If True (default), exclude suppressed messages.
        """
        return self._messages.get_messages(limit, suppress=suppress)

    def get_config(self) -> SignConfig:
        return self._config

    def get_effects_settings(self) -> EffectsSettings:
        """Live reference to the effects-settings block (rotation + pacing)."""
        return self._config.effects_settings

    def get_text_settings(self) -> TextSettings:
        """Live reference to the text-settings block (color, speed)."""
        return self._config.text_settings
