"""Browser app-scoped entry point — PyScript runtime loads this from `base.html`.

The admin app loads on every authenticated page. The script owns three
app-scoped singletons that any page can reach through the `window`:

  - `window._message_manager` — `MessageManager(is_browser=True)` with an
    `on_change` callback that (1) applies the new config to the
    app-scoped coordinator (`_coordinator.apply_settings(...)`) and
    (2) fans out to the JS-side `_dispatchChange` (preserves the
    `window.App.registerOnChange` API for /preview and /testing).
    Holds the in-browser copy of the SignConfig and the message
    ring buffer — the single source of truth for both.

  - `window._coordinator` — `EffectsCoordinator(message_manager=...)`
    constructed WITHOUT a render layer. The /preview page
    (`preview_main.py`) creates its page-local canvas + scroller +
    effects and calls `window._coordinator.bind(...)` once they're
    in scope. The coordinator is app-scoped (it survives across
    SPA navigations within the page load) but the render layer is
    page-scoped (the canvas only exists on /preview). The manager
    reference is set at construction time, not by `bind(...)`.

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

print("[app-py] module evaluation START (line 45)")
await loadPackage(["micropip", "tzdata"])  # type: ignore[reportGeneralTypeIssues]  # top-level await — see note above
print("[app-py] loadPackage complete (line 48)")

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

# The MessageManager / EffectsCoordinator / EventLog are imported
# lazily inside `dashboard_bootstrap.py` — they require lib_shared
# path setup that the bootstrap module owns. Importing them here
# would double-load and trigger the PyScript shared-globals issue
# (`_message_manager` clobbering).
from dashboard_controller import DashboardController
from dashboard_bootstrap import install_bootstrap

print("[app-py] dashboard_controller / dashboard_bootstrap imported")


# ---------------------------------------------------------------------------
# Python `logging` → browser console
# ---------------------------------------------------------------------------
#
# PyScript 2024.9.x captures `print(...)` calls and routes them to the
# browser console (you see `[app-py]` prefixed lines). Python's `logging`
# module is separate — by default its records go nowhere visible. Without
# this wire-through, the diagnostic lines in `lib_shared/` (e.g.
# `[debug-dispatch] CONFIG_ENVELOPE_RECEIVED`, `[select-mq] ENQUEUED`,
# `MQTT_INCOMING`) silently disappear, and the operator has no way to
# distinguish "Python never saw the envelope" from "Python saw it but
# downstream parsing failed".
#
# The handler below is a thin StreamHandler that re-emits every record
# as `console.log`. Severity → console method (`warning` → warn, etc.)
# so the records get the right icon in DevTools. Format is the standard
# `%(name)s-%(levelname)s-%(message)s` so a single grep on
# `[debug-dispatch]` still finds dispatch lines.
import logging as _logging  # type: ignore[import-not-found]


class _BrowserConsoleHandler(_logging.Handler):
    def emit(self, record: _logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            method = {
                _logging.DEBUG: js.console.debug,
                _logging.INFO: js.console.log,
                _logging.WARNING: js.console.warn,
                _logging.ERROR: js.console.error,
                _logging.CRITICAL: js.console.error,
            }.get(record.levelno, js.console.log)
            method(f"[py-log] {msg}")
        except Exception:
            pass  # never let logging itself break the page

    def format(self, record: _logging.LogRecord) -> str:
        # Mirror the PyScript `print(...)` line shape: logger-line-message.
        return f"{record.name}-{record.lineno}-{record.getMessage()}"


_browser_handler = _BrowserConsoleHandler()
_browser_handler.setLevel(_logging.INFO)
_logging.getLogger().addHandler(_browser_handler)
_logging.getLogger().setLevel(_logging.INFO)


# ---------------------------------------------------------------------------
# Build the per-generation runtime via the controller (issue #48).
# ---------------------------------------------------------------------------
#
# History: this module previously constructed the MessageManager,
# EffectsCoordinator, MQTT-WS client, and `_on_envelope_js` /
# `_on_status_js` callbacks as module-lifetime singletons. The
# standalone-preview-dashboard change replaces those singletons
# with a per-generation runtime under a `DashboardController`.
#
# What survives here:
#   - The `_app_config()` helper reads the server-inlined
#     `window.APP_CONFIG` block.
#   - The Python `logging` → browser-console wire-through (the
#     diagnostic prefix `[py-log]` lines).
#   - The auto-Start at page-load time: `controller.start()` is
#     called once `app_main.py` finishes evaluating. The dashboard
#     page wires Start/Stop button clicks to `controller.start()` /
#     `controller.stop()` directly — those calls go through the
#     same `on_start` / `on_stop` hooks installed below.
#
# What moved to `dashboard_bootstrap.py`:
#   - The MessageManager / EffectsCoordinator / MQTT-WS client
#     construction (per-generation).
#   - The `_on_change_js` / `_on_envelope_js` / `_on_status_js`
#     callbacks (per-generation, with generation discriminator
#     guards).
#   - The `window._seed` / `_hydrate_from_cache` /
#     `App.getMessages` / `App.getConfig` proxies.
#
# What moved to `dashboard_controller.py`:
#   - The state machine (stopped / starting / running / stopping /
#     error) and the `_teardown_generation` resource release.

from dashboard_controller import DashboardController
from dashboard_bootstrap import install_bootstrap

print("[app-py] constructing DashboardController")
_controller = DashboardController()
print("[app-py] calling install_bootstrap")
install_bootstrap(_controller)
print("[app-py] install_bootstrap RETURNED; render loop hooks installed")

# Expose the controller on `window` so the dashboard page's Start /
# Stop buttons can drive it. `window.Dashboard.start()` / `.stop()`
# are the production button-click bindings.
js.window.Dashboard = _controller
print(f"[app-py] window.Dashboard exposed (state={_controller.state()})")


# ---------------------------------------------------------------------------
# Auto-Start at page-load (legacy behavior).
# ---------------------------------------------------------------------------
#
# The pre-#48 page lifecycle did the network seed from `static/app.js`
# (`init()` calls `window._seed()`). That JS shim still works — the
# `_seed` proxy is installed by `install_bootstrap`'s `on_start` hook
# the moment the first Start completes. The JS side can wait for it
# the same way `preview_main.py` waits for `window._coordinator`.
#
# We do NOT auto-Start here from Python — the JS side drives the
# first Start so any race with the page's own initialization is
# visible at the JS layer (the same layer that has the rAF tick and
# the canvas). `static/app.js` will call `window.Dashboard.start()`
# once the controller is exposed and `window._seed` is awaited.


# Module evaluation complete — the controller is exposed; the
# first generation is built by `static/app.js` calling
# `window.Dashboard.start()`.
print("[app-py] module evaluation COMPLETE; controller exposed as window.Dashboard")
