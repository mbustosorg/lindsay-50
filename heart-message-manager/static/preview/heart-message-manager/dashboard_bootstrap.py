"""Per-generation dashboard runtime bootstrap — issue #48.

This module owns the work of constructing ONE generation's runtime:

  - shared Python `MessageManager(is_browser=True, on_change=...)`
  - shared Python `EffectsCoordinator(message_manager=...)` with
    a browser-side selector + the in-memory `EventLog`
  - the browser-side MQTT-over-WebSocket client, wired to the
    MessageManager's `dispatch()` and a status callback
  - the `window._seed` proxy that triggers `await seed()`

The bootstrap is registered as the controller's `on_start` hook.
`Stop` then walks the controller's teardown, which:

  - calls `mqtt_ws_client.close()` (the shim closes the WS and
    refuses to reconnect)
  - swaps `runtime.message_manager` to `_NullMessageManager` so a
    late MQTT envelope lands on the no-op stand-in
  - calls `event_log.clear()` so the prior queue is wiped
  - drops references to the coordinator, scroller, canvas, etc.
  - clears `window._coordinator` / `window._message_manager` /
    `window._mqtt_ws_client` so a stale `preview.js` tick reads
    `None` instead of a torn-down object
  - nulls `_on_change_js` so the App dispatcher fans out an
    `error`-state event

The PyScript-side `app_main.py` is a thin bootstrap that installs
this hook on the controller and auto-Starts a generation at
page-load time. The button-driven Start/Stop on the dashboard
calls `controller.start()` / `controller.stop()` directly.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any, Optional

import js  # type: ignore[import-not-found]
from pyodide.ffi import create_proxy, to_js  # type: ignore[import-not-found]

# Make `lib_shared` and the in-browser mirror of `heart-message-manager`
# importable. PyScript 2024.9.x's `[files]` handler writes each entry at
# the URL path; the parent of each package directory belongs in
# `sys.path`, so a plain `import lib_shared` resolves as a package.
#
# `py-config.toml` declares the same paths as `app_main.py` did
# pre-#48 — the file mappings (`/static/preview/heart-message-manager`
# etc.) are unchanged. The bootstrap module owns the path setup now
# because it owns the heavy imports.
for _path in (
    "/",
    "/static/preview",
    "/static/preview/heart-message-manager",
    "/static/preview/lib_shared",
):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from event_log import EventLog
from lib_shared.effects_coordinator import EffectsCoordinator
from lib_shared.message_manager import MessageManager
from lib_shared.selector import WeightedSelector

if TYPE_CHECKING:
    from dashboard_controller import DashboardController  # noqa: F401

log = logging.getLogger("heart")


def _app_config() -> dict:
    """Read the server-inlined `window.APP_CONFIG` block.

    Returns an empty dict on missing/corrupt config — the bootstrap
    then constructs a no-network runtime that surfaces a clear
    "missing APP_CONFIG" error.
    """
    raw = getattr(js.window, "APP_CONFIG", None)
    if raw is None:
        return {}
    try:
        return dict(raw.to_py() if hasattr(raw, "to_py") else raw)
    except Exception:
        return {}


def _install_js_callbacks(dash, runtime) -> None:
    """Bind the per-generation JS-callable callbacks onto runtime.proxies.

    Two objects are involved here, and the names matter:
      - `dash` is the per-generation `DashboardRuntime` — it owns
        the `seed_coroutine` / `get_messages_js` / `get_config_js`
        methods that bridge into PyScript's asyncio runtime.
      - `runtime` is the controller's `Runtime` dataclass — it owns
        the `proxies` dict (held so `Stop` can release the proxies
        in one place) and the canonical `message_manager` /
        `coordinator` fields that `_expose_window_globals` mirrors
        onto `window.*`.

    The proxies are generation-gated via `_gated(...)` so a late
    call from a torn-down generation short-circuits before it
    touches `runtime.message_manager`. The proxy objects still
    live for the lifetime of the underlying JS call site (PyScript
    can't unregister them), but the gate ensures the wrapped
    callable no-ops if the captured generation is no longer
    active. The runtime record's `_NullMessageManager` swap at
    Stop is defense-in-depth, not the primary correctness
    mechanism — design §2 prescribes the wrap-once closure.
    """
    gen_id = dash.generation_id
    runtime.proxies["seed"] = create_proxy(_gated(dash.seed_coroutine, gen_id))
    runtime.proxies["get_messages"] = create_proxy(_gated(dash.get_messages_js, gen_id))
    runtime.proxies["get_config"] = create_proxy(_gated(dash.get_config_js, gen_id))


def _expose_window_globals(runtime) -> None:
    """Set `window._coordinator` / `_message_manager` / `_mqtt_ws_client`.

    Each is the per-generation instance. Stop nulls them so the
    preview's polling-and-bind loop reads `None` after teardown.
    """
    js.window._message_manager = runtime.message_manager
    js.window._coordinator = runtime.coordinator
    if runtime.mqtt_ws_client is not None:
        js.window._mqtt_ws_client = runtime.mqtt_ws_client
    else:
        try:
            del js.window._mqtt_ws_client
        except Exception:
            pass
    js.window._seed = runtime.proxies["seed"]


def _clear_window_globals() -> None:
    """Clear `window._coordinator` / `_message_manager` / `_mqtt_ws_client`.

    Called from the controller's `stop()` teardown — the preview's
    rAF tick will read `None` from the globals and become a no-op.
    """
    for name in ("_coordinator", "_message_manager", "_mqtt_ws_client", "_seed"):
        try:
            setattr(js.window, name, None)
        except Exception:
            try:
                delattr(js.window, name)
            except Exception:
                pass


def _gated(callback, gen_id: int):
    """Wrap a callback with a wrap-once generation gate (design §2).

    The returned closure captures `gen_id` at construction time and
    consults the controller's active-generation registry at call
    time. If the active generation id does not match the captured
    one, the wrapper returns `None` without invoking the underlying
    callback; otherwise it forwards `*args, **kwargs`.

    Used at every PyScript proxy registration site so the captured
    `gen_id` is fixed for the lifetime of the proxy and cannot drift
    across Stop-then-Start boundaries. Per
    `feedback_one_shot_guards_need_discriminator.md` — pair every
    "is this the active generation?" check with the id of the
    thing whose side-effect we want to gate.
    """
    captured_gen = gen_id

    def _wrap(*args, **kwargs):
        ctrl = DashboardRuntime._controller
        if ctrl is None or not ctrl.is_active_generation(captured_gen):
            return None
        return callback(*args, **kwargs)

    return _wrap


# --- Per-generation runtime ------------------------------------------------


class DashboardRuntime:
    """One generation's dashboard runtime.

    Holds the MessageManager, EffectsCoordinator, EventLog, MQTT-WS
    client, and the JS-callable proxies that `window._seed` /
    `_get_messages_js` / `_get_config_js` route to. The
    `DashboardController.start()` hook constructs one of these,
    populates `Runtime.message_manager` / `coordinator` /
    `mqtt_ws_client` / `event_log` with the constructed objects,
    and flips the runtime state to `running`.

    `Stop` calls `_teardown_generation` (in `dashboard_controller.py`)
    which releases everything: closes the MQTT-WS client, swaps
    the MessageManager for the null stand-in, clears the event log,
    nulls the `window.*` globals, and drops all the per-generation
    references.
    """

    # Class-level, shared across all generations of the same browser
    # session — `app_main.py` builds exactly one DashboardBootstrap
    # and registers its hooks on a single DashboardController.
    _controller: Optional["DashboardController"] = None
    _config: dict = {}

    def __init__(self, generation_id: int, cfg: dict, runtime_record):
        self.generation_id = generation_id
        self.cfg = cfg
        self.runtime_record = runtime_record
        # Per-generation JS-side on_change callback (parameterless).
        # On every MessageManager change (REST seed completion, MQTT
        # dispatch, suppression toggle, config update), the proxy
        # fans out to `window.App._dispatchChange()` if present.
        self._on_change_js = self._make_on_change_js()
        # Generation-gated wrappers — captured once at construction,
        # used at every registration site (MQTT-WS onEnvelope/onStatus,
        # JS proxy slot bindings). See `_gated(...)` for the gate
        # semantics. The captured `gen_id` is a closure-local variable
        # bound at wrap time and cannot drift across Stop-then-Start
        # boundaries — per design §2 and
        # `feedback_one_shot_guards_need_discriminator.md`.
        self._on_envelope_gated = _gated(self._on_envelope_js, generation_id)
        self._on_status_gated = _gated(self._on_status_js, generation_id)

    # --- Hooks called by DashboardController -----------------------------

    def seed_coroutine(self) -> Any:
        """Await-able coroutine bound to `window._seed`."""
        return self._seed_async()

    def get_messages_js(self, limit: int = 100, suppress: bool = True) -> Any:
        return self._get_messages_js_impl(limit, suppress)

    def get_config_js(self) -> Any:
        return self._get_config_js_impl()

    # --- Async seeds / read APIs -----------------------------------------

    async def _seed_async(self) -> None:
        if self.runtime_record.message_manager is None:
            return
        try:
            await self.runtime_record.message_manager.seed()
        except Exception as e:
            log.warning("seed failed: %r", e)

    def _get_messages_js_impl(self, limit: int, suppress: bool):
        mm = self.runtime_record.message_manager
        if mm is None:
            return to_js([])
        try:
            entries = mm.get_messages(limit=limit, suppress=suppress)
            out = []
            for entry in entries:
                from lib_shared.models import Message  # local import; PyScript-side

                msg = getattr(entry, "message", None)
                if isinstance(msg, Message):
                    d = dict(msg.to_dict())
                elif isinstance(entry, dict):
                    d = dict(entry)
                else:
                    d = {}
                d["source"] = getattr(entry, "source", "rest")
                d["suppressed"] = bool(getattr(entry, "suppressed", False))
                rules = getattr(entry, "rules", None) or []
                d["rules"] = [r.to_dict() if hasattr(r, "to_dict") else r for r in rules]
                d["sender_name"] = getattr(entry, "sender_name", "") or ""
                d["display_time"] = getattr(entry, "display_time", "") or ""
                out.append(d)
            return to_js(out)
        except Exception:
            return to_js([])

    def _get_config_js_impl(self):
        mm = self.runtime_record.message_manager
        if mm is None:
            return to_js({})
        try:
            cfg = mm.get_config()
            from lib_shared.models import SignConfig  # local import

            return to_js(cfg.to_dict() if isinstance(cfg, SignConfig) else dict(cfg))
        except Exception:
            return to_js({})

    def _make_on_change_js(self):
        """Build a closure that consults the generation discriminator.

        The closure holds `generation_id` so even if the controller's
        active generation changes (Stop-then-Start), a delayed
        MessageManager callback from the prior generation short-
        circuits without mutating JS-side state.
        """

        gen_id = self.generation_id

        def _on_change():
            try:
                # The controller gates via the active generation id.
                ctrl = self._controller
                if ctrl is None or not ctrl.is_active_generation(gen_id):
                    return
                app = getattr(js.window, "App", None)
                if app is not None and hasattr(app, "_dispatchChange"):
                    app._dispatchChange()
            except Exception as e:
                log.warning("_on_change_js failed: %r", e)

        return _on_change

    def _on_envelope_js(self, raw) -> None:
        """MQTT-WS shim → Python: forward the envelope string to the
        MessageManager."""
        try:
            mm = self.runtime_record.message_manager
            if mm is None:
                return
            mm.dispatch(str(raw))
        except Exception as e:
            log.warning("dispatch failed: %r", e)

    def _on_status_js(self, state, detail) -> None:
        """MQTT-WS shim → Python: status event from the broker.

        Updates the page-level `#mqtt-status` element the admin UI
        drives. The dashboard's pill is independent — see
        `static/sign_status.js`.
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


# --- Bootstrap (the controller hook) ---------------------------------------


def install_bootstrap(controller) -> None:
    """Install the per-generation bootstrap hook on `controller`.

    The hook constructs one `DashboardRuntime` for each generation,
    wires the MQTT-WS client, exposes `window._coordinator` /
    `_message_manager` / `_mqtt_ws_client`, and installs the
    App.getMessages / getConfig proxies. The controller's
    `stop()` teardown calls `_clear_window_globals()` to null them
    out.

    `controller` is a `DashboardController` instance. The
    bootstrap holds a class-level reference so the per-generation
    `_on_change_js` closures can consult the active generation.
    """
    DashboardRuntime._controller = controller
    DashboardRuntime._config = _app_config()

    def on_start(runtime) -> None:
        print(f"[bootstrap-py] on_start ENTER generation={runtime.generation_id}")
        gen_id = runtime.generation_id
        cfg = DashboardRuntime._config
        dash = DashboardRuntime(gen_id, cfg, runtime)

        # Per-generation in-memory EventLog (issue #48, §2.6).
        print("[bootstrap-py] constructing EventLog")
        runtime.event_log = EventLog(max_entries=100)
        print(f"[bootstrap-py] EventLog OK id={id(runtime.event_log)}")

        # Per-generation MessageManager — wired with on_change that
        # consults the generation discriminator.
        print("[bootstrap-py] constructing MessageManager")
        runtime.message_manager = MessageManager(
            messages_api_url=str(cfg.get("messagesApiUrl") or ""),
            config_api_url=str(cfg.get("configApiUrl") or ""),
            api_key=str(cfg.get("apiKey") or ""),
            is_browser=True,
            on_change=create_proxy(dash._make_on_change_js()),
        )
        print(f"[bootstrap-py] MessageManager OK id={id(runtime.message_manager)}")

        # Per-generation EffectsCoordinator — wired with the
        # selector + the new event log.
        print("[bootstrap-py] constructing EffectsCoordinator")
        runtime.coordinator = EffectsCoordinator(
            message_manager=runtime.message_manager,
            media_api_base_url=str(js.window.location.origin),
            media_cache_dir="",
            is_browser=True,
            selector=WeightedSelector(),
            event_log=runtime.event_log,
        )
        print(f"[bootstrap-py] EffectsCoordinator OK id={id(runtime.coordinator)}")

        # Per-generation MQTT-WS client.
        mqtt_url = str(cfg.get("mqttWsUrl") or "")
        print(f"[bootstrap-py] mqtt_url={mqtt_url!r}")
        if mqtt_url:
            client_opts = {
                "url": mqtt_url,
                "username": str(cfg.get("mqttUsername") or ""),
                "password": str(cfg.get("mqttPassword") or ""),
                "topic": str(cfg.get("mqttTopic") or ""),
                "longDisconnectMs": int(cfg.get("mqttLongDisconnectMs") or 300000),
                # Generation-gated via `_gated(...)`: a late envelope
                # callback from a torn-down generation short-circuits
                # before touching `runtime.message_manager`. The
                # underlying PyScript proxies cannot be unregistered,
                # but the gate makes the wrapped callable a no-op when
                # the captured generation is no longer active.
                "onEnvelope": create_proxy(dash._on_envelope_gated),
                "onStatus": create_proxy(dash._on_status_gated),
            }
            client_opts_js = to_js(client_opts, dict_converter=js.Object.fromEntries)
            from js import createMqttWsClient  # type: ignore[import-not-found]

            print("[bootstrap-py] creating MQTT-WS client")
            runtime.mqtt_ws_client = createMqttWsClient(client_opts_js)
            print(f"[bootstrap-py] MQTT-WS client created id={id(runtime.mqtt_ws_client)}; starting")
            runtime.mqtt_ws_client.start()
            print("[bootstrap-py] MQTT-WS client.start() returned")

        # Bind the JS-callable proxies (seed / getMessages / getConfig)
        # and expose `window._coordinator` etc.
        print("[bootstrap-py] installing JS callbacks + exposing window globals")
        _install_js_callbacks(dash, runtime)
        _expose_window_globals(runtime)
        print("[bootstrap-py] window._coordinator / _message_manager / _seed EXPOSED")

        # Bridge App.getMessages / App.getConfig so existing per-page
        # JS can read from the in-memory ring without owning IndexedDB.
        app = getattr(js.window, "App", None)
        if app is not None:
            app.getMessages = runtime.proxies["get_messages"]
            app.getConfig = runtime.proxies["get_config"]
            if not hasattr(app, "_dispatchChange"):
                try:
                    app._dispatchChange = app.dispatchChange
                except Exception:
                    pass

        log.info("dashboard runtime generation=%d constructed", gen_id)
        print(f"[bootstrap-py] on_start COMPLETE generation={gen_id}")

    def on_stop(runtime) -> None:  # noqa: ARG001
        _clear_window_globals()
        log.info("dashboard runtime generation=%d torn down", runtime.generation_id)

    controller.set_render_loop_hooks(on_start=on_start, on_stop=on_stop)
