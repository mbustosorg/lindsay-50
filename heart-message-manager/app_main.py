"""Browser app-scoped entry point — PyScript runtime loads this from `base.html`.

The admin app loads on every authenticated page. The script owns three
app-scoped singletons that any page can reach through the `window`:

  - `window._message_manager` — `MessageManager(is_browser=True)` with an
    `on_change` callback that fans out to the JS-side `_dispatchChange`
    (preserves the `window.App.registerOnChange` API for /preview and
    /testing). Holds the in-browser copy of the SignConfig and the
    message ring buffer.

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

await loadPackage(["micropip", "tzdata"])  # type: ignore[reportGeneralTypeIssues]  # top-level await — see note above

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
from lib_shared.models import SignConfig

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


def _on_change_js() -> None:
    """MessageManager → JS: fan out the universal change event.

    The callback is parameterless. The JS-side `App` shim
    (`static/app.js`) maintains a list of per-page listeners
    registered via `App.registerOnChange(cb)`. Each listener
    re-renders whatever on its page could be affected by a
    state change (the page's `reRender` aggregator, typically).
    """
    try:
        app = getattr(js.window, "App", None)
        if app is not None and hasattr(app, "_dispatchChange"):
            app._dispatchChange()
    except Exception as e:
        print(f"[app_main] _on_change_js failed: {e!r}")


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
            # Defensive: an entry can be a MessageView (normal path)
            # or, in odd cases, a raw dict (e.g. a partial seed that
            # stored dicts instead of Message objects). Handle both.
            if hasattr(entry, "message"):
                d = entry.message.to_dict() if hasattr(entry.message, "to_dict") else dict(entry.message)
            else:
                d = dict(entry) if isinstance(entry, dict) else {}
            d["source"] = getattr(entry, "source", "rest")
            d["suppressed"] = bool(getattr(entry, "suppressed", False))
            rules = getattr(entry, "rules", None) or []
            d["rules"] = [r.to_dict() if hasattr(r, "to_dict") else r for r in rules]
            d["sender_name"] = getattr(entry, "sender_name", "") or ""
            d["display_time"] = getattr(entry, "display_time", "") or ""
            out.append(d)
        return to_js(out)
    except Exception as e:
        import traceback

        print(
            f"[app_main] _get_messages_js failed: {e!r}\n" f"{traceback.format_exc()}",
            flush=True,
        )
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
    """Network seed from the Flask REST API.

    Two callers:
    - `static/app.js init()` — on a first page load this
      tab (no sessionStorage cache). The seed populates the
      in-memory MessageManager, clears the (empty) cache,
      and writes a fresh cache via the trailing
      `_emit_change()`. After this, the WS connection keeps
      the cache current; subsequent navigations within the
      tab take the cache path (see `app.js init()`).
    - The Testing page's Refresh button — the user
      explicitly asked for a fresh network pull. Wipes the
      in-memory buffer + the sessionStorage cache (via
      `seed()`'s built-in clears) and re-fetches. The
      trailing `_emit_change` writes the new cache, so the
      next page navigation reflects the freshly-seeded data,
      not the pre-Refresh state.

    The MessageManager swallows per-endpoint failures
    internally, so a partial seed is non-fatal.
    """
    if _message_manager is None:
        return
    try:
        await _message_manager.seed()
    except Exception as e:
        print(f"[app_main] seed failed: {e!r}")


async def _hydrate_from_cache() -> bool:
    """Populate the in-browser MessageManager from sessionStorage.

    Bound to `window._hydrate_from_cache`. Called by
    `static/app.js`'s `init()` on every page load BEFORE the
    network seed. Returns True on a successful hit (the
    page renders the cached state on the first frame, no
    network call). Returns False on miss / corruption /
    version mismatch / sign mismatch — the caller should
    fall back to `window._seed()` in that case.

    Browser-only no-op (returns False) — the Pi has no
    sessionStorage. The MessageManager itself gates on
    `is_browser=True`.
    """
    if _message_manager is None:
        return False
    try:
        return await _message_manager.hydrate_from_cache()
    except Exception as e:
        print(f"[app_main] hydrate_from_cache failed: {e!r}")
        return False


# ---------------------------------------------------------------------------
# Build the app-scoped singletons.
# ---------------------------------------------------------------------------

_cfg = _app_config()

_message_manager = MessageManager(
    messages_api_url=str(_cfg.get("messagesApiUrl") or ""),
    config_api_url=str(_cfg.get("configApiUrl") or ""),
    api_key=str(_cfg.get("apiKey") or ""),
    is_browser=True,
    on_change=create_proxy(_on_change_js),
)

_coordinator = EffectsCoordinator()

_mqtt_ws_client = None
_mqtt_ws_url = str(_cfg.get("mqttWsUrl") or "")
if _mqtt_ws_url:
    _client_opts = {
        "url": _mqtt_ws_url,
        "username": str(_cfg.get("mqttUsername") or ""),
        "password": str(_cfg.get("mqttPassword") or ""),
        "topic": str(_cfg.get("mqttTopic") or ""),
        "longDisconnectMs": int(_cfg.get("mqttLongDisconnectMs") or 300000),
        "onEnvelope": create_proxy(_on_envelope_js),
        "onStatus": create_proxy(_on_status_js),
    }
    # `to_js` converts the Python dict to a real JS object — required
    # because the JS shim's `createMqttWsClient({url, topic, ...})`
    # destructures its argument. A bare Python dict crosses the
    # Pyodide boundary as a JsProxy, and JS destructuring on a
    # JsProxy silently yields `undefined` for every key (the live
    # symptom was `new WebSocket(undefined, ...)` resolving to
    # `ws://localhost:3100/undefined` because the browser
    # stringifies `undefined` into the URL). `to_js` with
    # `dict_converter=js.Object.fromEntries` produces a plain
    # JS object whose properties the destructuring can read.
    _client_opts_js = to_js(_client_opts, dict_converter=js.Object.fromEntries)
    _mqtt_ws_client = createMqttWsClient(_client_opts_js)
    # Start the WS connection; the shim handles reconnect / pause /
    # status internally. Any envelope that lands calls
    # `_message_manager.dispatch(raw)` which fires `_on_change_js`
    # which fans out to `window.App._dispatchChange`.
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
js.window._hydrate_from_cache = create_proxy(_hydrate_from_cache)
# JS-side read APIs — preserve the `window.App.getMessages` /
# `getConfig` surface so `testing.html` (and any future per-page
# hydration path in `preview.js`) can read from the in-memory ring
# buffer / SignConfig without owning IndexedDB.
_app = getattr(js.window, "App", None)
if _app is not None:
    _app.getMessages = create_proxy(_get_messages_js)
    _app.getConfig = create_proxy(_get_config_js)
    # The MessageManager's on_change callback (via the proxy
    # `_on_change_js` above) calls this; per-page listeners
    # registered via `App.registerOnChange` are reached through
    # the JS-side `dispatchChange` fan-out.
    if not hasattr(_app, "_dispatchChange"):
        try:
            _app._dispatchChange = _app.dispatchChange
        except Exception:
            pass
