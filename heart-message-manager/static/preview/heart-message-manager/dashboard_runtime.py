"""Page-load dashboard runtime — issue #48, simplified in 2026-07-23.

Owns the singletons that the browser-side dashboard depends on:

  - `message_manager`        `MessageManager(is_browser=True)`
  - `coordinator`            `EffectsCoordinator(message_manager=...)`,
                             bound to a fresh `EventLog`
  - `mqtt_ws_client`         browser-side MQTT-over-WebSocket shim,
                             piped through to `MessageManager.dispatch`
  - `seed()` proxy           `await seed()` for the in-memory ring-buffer
                             hydrate

The runtime installs ONCE per page load (PyScript module evaluation
finishes once). Refresh the page to restart — there is no Start/Stop
toggle, no per-generation discriminator, no controller state machine.
The previous `DashboardController` (with `start/stop/restart` and a
generation id discriminator) was deleted because the operator doesn't
need to pause and resume the simulator; refresh does the same job with
none of the gating complexity.

`install_runtime` is called exactly once by `app_main.py` at module
evaluation time. It exposes the four objects on `window`:

  - `window._message_manager`
  - `window._coordinator`
  - `window._mqtt_ws_client`
  - `window._seed`

`preview_main.py` reads `_coordinator` and binds the canvas +
scroller + effects + heart onto it. `app.js` reads
`_message_manager` for the JS-side `App.getMessages` / `getConfig`
proxies and `_seed` for the load-time hydrate.

MQTT-WS status events (`onStatus`) update the page-level
`#preview-mqtt-pill` summary so the operator sees connection state
from the Preview card itself. The legacy `#mqtt-status` element is
not emitted by the dashboard template; we keep a no-op fallback write
for any page that still hosts it (defensive — empty guard).
"""
from __future__ import annotations

import logging
import sys
from typing import Any, Optional

import js  # type: ignore[import-not-found]
from pyodide.ffi import create_proxy, to_js  # type: ignore[import-not-found]

# Make `lib_shared` + the in-browser mirror packages importable.
# PyScript 2024.9.x's `[files]` handler writes each entry at the
# URL path; the parent of each package directory belongs in
# `sys.path`, so plain `import lib_shared` resolves as a package.
# Same pattern `app_main.py` uses for its own imports.
for _path in (
    "/",
    "/static/preview",
    "/static/preview/heart-message-manager",
    "/static/preview/lib_shared",
):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from event_log import EventLog  # noqa: E402
from lib_shared.effects_coordinator import EffectsCoordinator  # noqa: E402
from lib_shared.message_manager import MessageManager  # noqa: E402
from lib_shared.selector import WeightedSelector  # noqa: E402

log = logging.getLogger("heart")

# Behavioral knobs (per `feedback_behavioral_knobs_in_code.md`).
DEFAULT_EVENT_LOG_MAX_ENTRIES = 100


def _app_config() -> dict:
    """Read the server-inlined `window.APP_CONFIG` block.

    Returns an empty dict on missing/corrupt config. Pyodide 0.26
    hands JS objects to Python as `PyProxy` of a Map-like object —
    `dict(raw)` works on it. Host CPython (tests) gets a plain dict.
    """
    raw = getattr(js.window, "APP_CONFIG", None)
    if raw is None:
        return {}
    try:
        if hasattr(raw, "to_py"):
            return dict(raw.to_py())
        return dict(raw)
    except Exception:
        return {}


def _on_envelope_py(raw: Any) -> None:
    """MQTT-WS shim → MessageManager: forward the envelope string."""
    try:
        mm = getattr(js.window, "_message_manager", None)
        if mm is None:
            return
        mm.dispatch(str(raw))
    except Exception as e:
        log.warning("dispatch failed: %r", e)


def _on_status_py(state: Any, detail: Any) -> None:
    """MQTT-WS shim → pill: status event from the envelope feed.

    Updates the Preview card's MQTT pill (`#preview-mqtt-pill`) so
    the operator sees WS connection state without scanning the
    page header. The pill text + dot color flip with the
    connection state; the WS URL + subscribe topic live in the
    pill's `title=` tooltip (set by `dashboard.html`, not here).

    Legacy `#mqtt-status` (rendered by `_mqtt_header.html`) gets a
    fallback label-only write so any page that still hosts it
    keeps working without script changes.
    """
    try:
        d = dict(detail.to_py() if hasattr(detail, "to_py") else detail) if detail is not None else {}
    except Exception:
        d = {}

    color_map = {
        "connected": ("bg-green-500", "Live"),
        "reconnecting": ("bg-amber-500", f"Reconnecting… (attempt {d.get('attempt', 0)})"),
        "paused": ("bg-amber-500", f"Paused ({int(d.get('elapsedMs', 0) / 1000)}s elapsed)"),
        "error": ("bg-red-500", f"Error{d.get('error') and f': {d['error']}'}"),
        "connecting": ("bg-slate-400", "Connecting…"),
        "disconnected": ("bg-slate-400", "Disconnected"),
    }
    bg_map = {
        "connected": "bg-green-100 text-green-700",
        "reconnecting": "bg-amber-100 text-amber-800",
        "paused": "bg-amber-100 text-amber-800",
        "error": "bg-red-100 text-red-700",
        "connecting": "bg-slate-100 text-slate-600",
        "disconnected": "bg-slate-100 text-slate-600",
    }
    state_str = str(state)
    try:
        dot_color, label = color_map.get(state_str, ("bg-slate-400", state_str.capitalize()))
        pill = js.document.getElementById("preview-mqtt-pill")
        if pill is not None:
            pill.dataset.state = state_str
            label_el = pill.querySelector("#preview-mqtt-label")
            if label_el is not None:
                label_el.textContent = label
            dot_el = pill.querySelector("span.rounded-full")
            if dot_el is not None:
                dot_el.className = f"w-2 h-2 {dot_color} rounded-full"
            pill_bg = bg_map.get(state_str, "bg-slate-100 text-slate-600")
            pill.classList.remove(
                "bg-green-100", "text-green-700",
                "bg-amber-100", "text-amber-800",
                "bg-red-100", "text-red-700",
                "bg-slate-100", "text-slate-600",
            )
            for cls in pill_bg.split():
                pill.classList.add(cls)
    except Exception:
        pass

    try:
        legacy = js.document.getElementById("mqtt-status")
        if legacy is not None:
            legacy.textContent = color_map.get(state_str, ("bg-slate-400", state_str.capitalize()))[1]
    except Exception:
        pass


# Proxies hold references to the underlying callables; PyScript's
# proxy registry keeps them alive for the lifetime of the page.
_envelope_proxy: Any = None
_status_proxy: Any = None


async def _seed_async() -> None:
    """Await-able coroutine bound to `window._seed`.

    Loads /api/messages into the in-memory ring buffer. Idempotent —
    the MessageManager clears first, so calling on every page load
    (and again from any caller that wants to re-hydrate) is safe.
    """
    mm = getattr(js.window, "_message_manager", None)
    if mm is None:
        return
    try:
        await mm.seed()
    except Exception as e:
        log.warning("seed failed: %r", e)


def install_runtime() -> None:
    """Build + expose the singletons. Idempotent — call once.

    Constructed objects:
      - `EventLog(max_entries=DEFAULT_EVENT_LOG_MAX_ENTRIES)` —
        the in-memory selector event log
      - `MessageManager(is_browser=True, on_change=...)` — fed by
        the per-page WS envelopes and the bootstrap seed
      - `EffectsCoordinator(message_manager=..., selector=...)` —
        binds the event log + per-frame coordinator
      - The browser-side MQTT-WS shim (`createMqttWsClient(...)`),
        wired to `_on_envelope_py` + `_on_status_py`

    The seed coroutine is exposed as `window._seed`; the seed
    itself is NOT auto-fired by `install_runtime` — the dashboard
    page's `app.js` awaits it once `_message_manager` is
    installed so the operator lands on a populated table.
    """
    global _envelope_proxy, _status_proxy
    cfg = _app_config()

    log.info("install_runtime: constructing EventLog")
    event_log = EventLog(max_entries=DEFAULT_EVENT_LOG_MAX_ENTRIES)

    log.info("install_runtime: constructing MessageManager")
    # The on_change callback fans out to JS-side listeners via
    # the universal dispatcher (`window.App._dispatchChange`).
    # Built lazily because `app.js` may not have installed it yet
    # — `_dispatchChange` is read inside the closure.
    def _on_change_js() -> None:
        try:
            app = getattr(js.window, "App", None)
            if app is not None and hasattr(app, "_dispatchChange"):
                app._dispatchChange()
        except Exception as e:
            log.warning("_on_change_js failed: %r", e)

    mm = MessageManager(
        messages_api_url=str(cfg.get("messagesApiUrl") or ""),
        config_api_url=str(cfg.get("configApiUrl") or ""),
        api_key=str(cfg.get("apiKey") or ""),
        is_browser=True,
        on_change=create_proxy(_on_change_js),
    )

    log.info("install_runtime: constructing EffectsCoordinator")
    coordinator = EffectsCoordinator(
        message_manager=mm,
        media_api_base_url=str(js.window.location.origin),
        media_cache_dir="",
        is_browser=True,
        selector=WeightedSelector(),
        event_log=event_log,
    )

    mqtt_ws_client: Optional[Any] = None
    mqtt_url = str(cfg.get("mqttWsUrl") or "")
    if mqtt_url:
        log.info("install_runtime: creating MQTT-WS client url=%s", mqtt_url)
        _envelope_proxy = create_proxy(_on_envelope_py)
        _status_proxy = create_proxy(_on_status_py)
        client_opts = {
            "url": mqtt_url,
            "username": str(cfg.get("mqttUsername") or ""),
            "password": str(cfg.get("mqttPassword") or ""),
            "topic": str(cfg.get("mqttTopic") or ""),
            "longDisconnectMs": int(cfg.get("mqttLongDisconnectMs") or 300000),
            "onEnvelope": _envelope_proxy,
            "onStatus": _status_proxy,
        }
        client_opts_js = to_js(client_opts, dict_converter=js.Object.fromEntries)
        from js import createMqttWsClient  # type: ignore[import-not-found]

        mqtt_ws_client = createMqttWsClient(client_opts_js)
        if mqtt_ws_client is not None:
            mqtt_ws_client.start()

    # Expose the singletons. `preview_main.py` reads `_coordinator`
    # to bind the canvas + scroller + effects. `app.js` reads
    # `_message_manager` for its `App.getMessages` /
    # `getConfig` proxies.
    js.window._message_manager = mm
    js.window._coordinator = coordinator
    js.window._event_log = event_log
    if mqtt_ws_client is not None:
        js.window._mqtt_ws_client = mqtt_ws_client
    js.window._seed = create_proxy(_seed_async)
