"""Browser app-scoped entry point — PyScript runtime loads this from `base.html`.

The admin app loads on every authenticated page. This module:

  1. Imports `dashboard_runtime.install_runtime()` — that constructs
     the singletons the dashboard depends on (MessageManager,
     EffectsCoordinator, EventLog, MQTT-WS client) and exposes them
     on `window` for downstream consumers (`preview_main.py`,
     `app.js`).

  2. Wires Python `logging` to `console.{log,warn,error}` so the
     diagnostic lines emitted by `lib_shared/` (e.g.
     `[debug-dispatch] CONFIG_ENVELOPE_RECEIVED`,
     `[select-mq] ENQUEUED`, `MQTT_INCOMING`) surface in the browser
     console instead of silently disappearing.

No canvas, no requestAnimationFrame, no per-frame work. All
per-frame work lives in `preview_main.py`.

History (issue #48, simplified 2026-07-23). Previously this module
constructed a `DashboardController` + per-generation bootstrap hooks
(`_gated` discriminators, `_teardown_generation`, start/stop
state machine). The 2026-07-23 simplification replaced those with
a single `install_runtime()` call — page load = Pi startup, refresh
to restart. The previous design's controller state machine plus
runtime-discriminator wrapping was about avoiding a Stop-then-Start
race; since Stop is no longer exposed (the operator just refreshes),
the wrapping is gone with it.

NOTE: no rgbmatrix import anywhere in this file or its imports.
Pillow + numpy are pulled in via the shared py-config.toml
declared packages; this script only needs the runtime + a JS
import to read `APP_CONFIG`.
"""

from pyodide_js import loadPackage  # type: ignore[reportGeneralTypeIssues]  # noqa: F401  (top-level await: PyScript 2024.9.x runs via `eval_code_async`)

print("[app-py] module evaluation START (line 47)")
await loadPackage(["micropip", "tzdata"])  # type: ignore[reportGeneralTypeIssues]  # top-level await — see note above
print("[app-py] loadPackage complete (line 49)")

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

# The runtime module owns the heavy imports (MessageManager +
# EffectsCoordinator + EventLog + lib_shared.effects_coordinator +
# lib_shared.message_manager). They are imported inside
# `dashboard_runtime.py` so this module stays lightweight.
from dashboard_runtime import install_runtime

print("[app-py] dashboard_runtime imported")


# ---------------------------------------------------------------------------
# Python `logging` → browser console
# ---------------------------------------------------------------------------
#
# PyScript 2024.9.x captures `print(...)` calls and routes them to the
# browser console (you see `[app-py]` prefixed lines). Python's `logging`
# module is separate — by default its records go nowhere visible. Without
# this wire-through, the diagnostic lines in `lib_shared/` silently
# disappear, and the operator has no way to distinguish "Python never
# saw the envelope" from "Python saw it but downstream parsing failed".
#
# Severity → console method (`warning` → warn, etc.) so the records
# get the right icon in DevTools. Format is the standard
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
# Build the page-load runtime (issue #48, simplified 2026-07-23).
# ---------------------------------------------------------------------------
#
# `install_runtime()` constructs the MessageManager, EffectsCoordinator,
# EventLog, MQTT-WS client, and the `_seed` proxy in one place, and
# exposes them on `window` so downstream consumers can pick them up:
#
#   - `preview_main.py` reads `_coordinator` to bind the canvas +
#     scroller + effects onto the per-frame coordinator
#   - `app.js` reads `_message_manager` and uses it as the source for
#     `App.getMessages` / `getConfig` proxies
#   - The MQTT-WS client's `onStatus` callback updates the page-level
#     `#preview-mqtt-pill` directly (see dashboard_runtime.py)
#
# There is no controller; there is no Start/Stop toggle; there is no
# generation discriminator. Page load = Pi startup; refresh = restart.
# The `_seed` proxy is exposed but NOT auto-fired from Python — the
# dashboard page's `app.js` awaits it once `_message_manager` is
# installed so the operator lands on a populated table.

print("[app-py] calling install_runtime")
install_runtime()
print("[app-py] install_runtime RETURNED; window globals ready")

# Module evaluation complete — the singleton runtime is up. The
# dashboard's rAF loop starts when `preview_main.py` runs (since
# that owns the canvas-bind step) and the in-memory buffer is
# seeded by `app.js` calling `window._seed()`.
print("[app-py] module evaluation COMPLETE; runtime installed")