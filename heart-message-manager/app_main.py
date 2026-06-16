"""Browser app-scoped entry point — PyScript runtime loads this from `base.html`.

The admin app loads on every authenticated page. The script owns three
app-scoped singletons that any page can reach through the `window`:

  - `window._message_manager` — `MessageManager(is_browser=True)` with an
    `on_message` callback that fans out to the existing JS-side
    `dispatchToCallbacks` (preserves the `window.App.registerOnMessageCallback`
    API for /preview and /testing). Holds the in-browser copy of the
    SignConfig and the message ring buffer.

  - `window._coordinator` — `EffectsCoordinator` constructed WITHOUT a
    render layer. The /preview page (`preview_main.py`) creates its
    page-local canvas + scroller + effects and calls
    `window._coordinator.bind(...)` once they're in scope. The
    coordinator is app-scoped (it survives across SPA navigations
    within the page load) but the render layer is page-scoped
    (the canvas only exists on /preview).

  - `window._mqtt_ws_client` — Python wrapper around the native JS
    MQTT-over-WebSocket shim. Starts the WS connection and forwards
    every envelope into `window._message_manager.dispatch(raw)`.

The seed trigger is `window._message_manager.seed()` (async coroutine).
It is called once per page load by `static/app.js` — that's the
"auth-aware" trigger: app.js only loads when the Flask session
cookie is valid (gated by `{% if current_user.is_authenticated %}` in
base.html), so the seed runs on every login and every full-page
reload. SPA navigations within the page load don't fire
DOMContentLoaded so the seed does not re-run; the in-memory state
is current for the lifetime of the page.

No canvas, no requestAnimationFrame, no per-frame work. All
per-frame work lives in `preview_main.py`.

NOTE: no rgbmatrix import anywhere in this file or its imports.
Pillow + numpy are pulled in via the shared py-config.toml
declared packages; this script only needs the runtime + a JS
import to read `APP_CONFIG`.
"""

from pyodide_js import loadPackage  # type: ignore[reportGeneralTypeIssues]  # noqa: F401  (top-level await: PyScript 2024.9.x runs via `eval_code_async`)

await loadPackage(["micropip"])  # type: ignore[reportGeneralTypeIssues]  # top-level await — see note above

import sys

# Make `lib_shared` and `heart-message-manager` importable inside the
# browser. PyScript 2024.9.x's [files] handler writes each entry at the
# URL path (not the key); the destination matches the URL declared in
# py-config.toml. The PARENT of each package dir is what belongs in
# `sys.path`, so plain `import lib_shared` resolves as a package.
for path in (
    "/",
    "/static/preview",
    "/static/preview/heart-message-manager",
    "/static/preview/lib_shared",
):
    if path not in sys.path:
        sys.path.insert(0, path)

# Imports from the in-browser render path.
import js
from pyodide.ffi import create_proxy, to_js  # type: ignore[import-not-found]

from lib_shared.message_manager import MessageManager
from lib_shared.effects_coordinator import EffectsCoordinator
from lib_shared.models import Message, SignConfig

# The existing JS-side `mqtt_ws_client.js` shim is loaded by `base.html`
# before this script runs. We import it via the `js` global and wrap it
# so the on-envelope / on-status callbacks can call into Python.
from js import createMqttWsClient  # type: ignore[import-not-found]


def _app_config() -> dict:
    """Read the server-inlined `window.APP_CONFIG` block.

    `base.html`'s context processor populates it from `settings.toml`.
    Pyodide hands it across as a JsProxy; coerce to a plain dict.
    """
    raw = getattr(js.window, "APP_CONFIG", None)
    if raw is None:
        return {}
    try:
        return dict(raw.to_py() if hasattr(raw, "to_py") else raw)
    except Exception:
        return {}


def _on_envelope_js(raw) -> None:
    """JS shim → Python: forward the envelope string to the MessageManager.

    The MQTT-WS shim passes the raw JSON string (the decoded
    PUBLISH payload). The MessageManager parses + dispatches it
    itself.
    """
    if _message_manager is None:
        return
    try:
        _message_manager.dispatch(str(raw))
    except Exception as e:  # never let an envelope bring down the page
        print(f"[app_main] dispatch failed: {e!r}")


def _on_status_js(state, detail) -> None:
    """JS shim → Python: status event from the broker.

    Forward to the page-level `#mqtt-status` element the admin UI
    already drives, so the Live / Reconnecting / Paused / Error
    pill stays in sync regardless of who owns the WS.
    """
    try:
        d = dict(detail.to_py() if hasattr(detail, "to_py") else detail) if detail is not None else {}
    except Exception:
        d = {}
    label_map = {
        "connected": "Live",
        "reconnecting": f"Reconnecting… (attempt {d.get('attempt', 0)})",
        "paused": f"Paused ({int(d.get('elapsedMs', 0) / 1000)}s elapsed)",
        "error": f"Error{d.get('error') and f': {d['error']}'}",
    }
    label = label_map.get(str(state), str(state).capitalize())
    try:
        el = js.document.getElementById("mqtt-status")
        if el is not None:
            el.textContent = label
    except Exception:
        pass


def _on_message_js(msg) -> None:
    """MessageManager → JS: fan out to the page-level callbacks.

    The MessageManager hands us a Python `Message`; the JS-side
    per-page callbacks (`registerOnMessageCallback`) expect a
    flat `Message.to_dict()` shape. Convert via `to_js` so the
    JS proxy sees a real object, not a JsProxy of a Python class.
    Config envelopes don't fire this callback (the MessageManager
    routes them through `_handle_config`); the live preview.js
    checks for `effect_settings` in the payload to distinguish.
    """
    try:
        payload = msg.to_dict() if isinstance(msg, Message) else dict(msg)
    except Exception:
        return
    try:
        app = getattr(js.window, "App", None)
        if app is not None and hasattr(app, "_dispatchToCallbacks"):
            app._dispatchToCallbacks(to_js(payload))
    except Exception as e:
        print(f"[app_main] _on_message_js failed: {e!r}")


async def _get_messages_js(limit: int = 100, suppress: bool = True) -> object:
    """JS-callable: return enriched message entries (newest first).

    Mirrors the old `messageBufferStore.hydrate()` contract so
    `testing.html` (and the future `preview.js` hydration path)
    can read from the in-memory ring buffer. Returns a list of
    flat dicts — the JS side does its own enrichment (or the
    same fields the Python `FilteredMessages._enrich_messages`
    computes; the two are intended to agree).
    """
    if _message_manager is None:
        return to_js([])
    try:
        entries = _message_manager.get_messages(limit=limit, suppress=suppress)
        out = []
        for entry in entries:
            d = entry.message.to_dict()
            d["source"] = entry.source
            d["suppressed"] = bool(entry.suppressed)
            d["rules"] = [r.to_dict() for r in (entry.rules or [])]
            d["sender_name"] = entry.sender_name or ""
            d["display_time"] = entry.display_time or ""
            out.append(d)
        return to_js(out)
    except Exception as e:
        print(f"[app_main] _get_messages_js failed: {e!r}")
        return to_js([])


async def _get_config_js() -> object:
    """JS-callable: return the current SignConfig as a plain dict."""
    if _message_manager is None:
        return to_js({})
    try:
        cfg = _message_manager.get_config()
        return to_js(cfg.to_dict() if isinstance(cfg, SignConfig) else dict(cfg))
    except Exception as e:
        print(f"[app_main] _get_config_js failed: {e!r}")
        return to_js({})


async def _seed() -> None:
    """Seed the in-memory MessageManager from the Flask REST API.

    Called once per page load by `static/app.js`'s `init()` (the
    "auth-aware" trigger — `app.js` only loads when
    `current_user.is_authenticated` is true, so the seed runs on
    every login and every full-page-reload). The MessageManager
    swallows per-endpoint failures internally, so a partial seed
    is non-fatal.
    """
    if _message_manager is None:
        return
    try:
        await _message_manager.seed()
    except Exception as e:
        print(f"[app_main] seed failed: {e!r}")


# ---------------------------------------------------------------------------
# Build the app-scoped singletons.
# ---------------------------------------------------------------------------

_cfg = _app_config()

_message_manager = MessageManager(
    messages_api_url=str(_cfg.get("messagesApiUrl") or ""),
    config_api_url=str(_cfg.get("configApiUrl") or ""),
    api_key=str(_cfg.get("apiKey") or ""),
    is_browser=True,
    on_message=_on_message_js,
)

_coordinator = EffectsCoordinator()

_mqtt_ws_client = None
_mqtt_ws_url = str(_cfg.get("mqttWsUrl") or "")
if _mqtt_ws_url:
    _mqtt_ws_client = createMqttWsClient(
        {
            "url": _mqtt_ws_url,
            "username": str(_cfg.get("mqttUsername") or ""),
            "password": str(_cfg.get("mqttPassword") or ""),
            "topic": str(_cfg.get("mqttTopic") or ""),
            "longDisconnectMs": int(_cfg.get("mqttLongDisconnectMs") or 300000),
            "onEnvelope": create_proxy(_on_envelope_js),
            "onStatus": create_proxy(_on_status_js),
        }
    )
    # Start the WS connection; the shim handles reconnect / pause /
    # status internally. Any envelope that lands calls
    # `_message_manager.dispatch(raw)` which fires `_on_message_js`
    # which fans out to `window.App._dispatchToCallbacks`.
    _mqtt_ws_client.start()


# Expose on `window` for the rest of the page to use. These are the
# names the design promises: `_message_manager`, `_coordinator`,
# `_mqtt_ws_client`. Also drop a `seed()` function on `window` so
# `static/app.js` (plain JS, no PyScript bridge needed) can call it.
js.window._message_manager = _message_manager
js.window._coordinator = _coordinator
if _mqtt_ws_client is not None:
    js.window._mqtt_ws_client = _mqtt_ws_client
js.window._seed = create_proxy(_seed)
# JS-side read APIs — preserve the old `window.App.getMessages` /
# `getConfig` surface so `testing.html` (and any future per-page
# hydration path in `preview.js`) can read from the in-memory ring
# buffer / SignConfig without owning IndexedDB.
_app = getattr(js.window, "App", None)
if _app is not None:
    _app.getMessages = create_proxy(_get_messages_js)
    _app.getConfig = create_proxy(_get_config_js)
    # The MessageManager's on_message callback fires this; per-page
    # listeners (e.g. `registerOnMessageCallback` from
    # `static/preview/preview.js`) stay subscribed.
    if not hasattr(_app, "_dispatchToCallbacks"):
        # Belt-and-suspenders: the existing app.js's `dispatchToCallbacks`
        # is a closure, not a method. Forward through `App` by aliasing.
        try:
            _app._dispatchToCallbacks = _app.dispatchToCallbacks
        except Exception:
            pass
