"""Browser entry point for the sign preview.

Runs in PyScript (Pyodide in the browser tab). Boots the WebCanvas /
WebDisplay, instantiates the effect cycle and scroller, wires up the
shared EffectsCoordinator, and exposes JS-callable functions:

    request_message(body)        — hand a new body to the coordinator
    tick()                       — advance one frame
    get_frame_rgba()             — read the current frame buffer
    get_current_text()           — read the active message body
    get_current_effect_name()    — read the active effect class name

The actual main loop lives in static/preview.js, which drives tick() via
requestAnimationFrame (capped at 30 FPS) and calls request_message() from
its setInterval poll.

NOTE: no rgbmatrix import anywhere in this file or its imports (Pillow +
numpy are pulled in via the py-config.toml declared packages).
"""

import sys

# Install runtime dependencies BEFORE importing the modules that need
# them. The Honeycomb effect uses numpy; PreviewScroller + WebCanvas
# use Pillow. Doing the install in py-config.toml's [packages] section
# would crash on PyScript 2024.9.x — see the comment in py-config.toml.
#
# `pyodide_js.loadPackage` (the JS-side loadPackage, exposed to Python
# via Pyodide's `pyodide_js` shim) is the supported way to pre-load
# packages in Pyodide 0.26. Calling micropip.install with the [packages]
# dict in py-config.toml passes a non-iterable JsProxy and crashes
# (`'pyodide.ffi.JsProxy' object is not iterable` from
# micropip/_commands/install.py:142), so we deliberately avoid that
# path. Top-level await is supported by PyScript 2024.9.x's py-script
# element (it runs the source via `eval_code_async`).
from pyodide_js import loadPackage

await loadPackage(["micropip", "numpy", "Pillow"])  # type: ignore[reportGeneralTypeIssues]  # top-level await: PyScript 2024.9.x runs via `eval_code_async`

# Make lib_shared, heart-matrix-controller, and heart-message-manager all
# importable. PyScript 2024.9.x's [files] handler writes each entry at
# the URL path (not the key), so the destination is the same as the
# URL we declared in py-config.toml — e.g.
# "/static/preview/heart-message-manager/preview_display.py". The PARENT
# of each package dir is what belongs in sys.path, so plain
# `import lib_shared` resolves as a package.
for path in (
    "/",
    "/static/preview",
    "/static/preview/heart-message-manager",
    "/static/preview/lib_shared",
):
    if path not in sys.path:
        sys.path.insert(0, path)

# Standard imports from the browser-side render path.
from preview_display import WebCanvas, WebDisplay
from preview_scroller import PreviewScroller
from lib_shared.effects_coordinator import EffectsCoordinator

# Standard bitmap patterns the browser preview can run (no filesystem
# assets, no OpenCV). PngDisplay / VideoDisplay stay Pi-only.
from lib_shared.patterns.fireworks import Fireworks
from lib_shared.patterns.flame import Flame
from lib_shared.patterns.nightsky import NightSky
from lib_shared.patterns.honeycomb import Honeycomb
from lib_shared.patterns.hyperspace import Hyperspace
from lib_shared.patterns.heartbeat import Heartbeat

# The 64x64 logical panel — source of truth matches the device.
PANEL_WIDTH = 64
PANEL_HEIGHT = 64

# --- Build the coordinator ---

_web_canvas = WebCanvas(PANEL_WIDTH, PANEL_HEIGHT)
_display = WebDisplay(_web_canvas)

# Effects list built by direct instantiation (no try/except hedging —
# every pattern imported here is known to work in the browser).
_effects = [
    Fireworks(_display),
    Flame(_display),
    NightSky(_display),
    Honeycomb(_display),
    Hyperspace(_display),
]
_scroller = PreviewScroller(_display)
_heart = Heartbeat(_display)
_coordinator = EffectsCoordinator(_display, _scroller, _effects, heart=_heart)

# Begin the boot splash (beating heart). The first preview.js poll hands in the
# latest message, which plays once the heart fades out — mirroring the device's
# "show the last seeded message at startup" behavior.
_coordinator.start(None)


# --- JS-callable surface ---
#
# PyScript 2024.9.x removed the `window.pyscript.globals.get("name")`
# bridge that older releases used. The supported way to expose Python
# functions to JS is to assign them to `js.window` (the Pyodide proxy
# for the browser's `window` object) from within Python. The JS side
# then calls them as plain functions on `window` — no `pyscript` global
# involved. The function bodies below are still defined later in the
# file; `_install_js_api()` is called at the very end so the names are
# in scope.
import js


def _install_js_api() -> None:
    js.window.tick = tick
    js.window.request_message = request_message
    js.window.get_frame_rgba = get_frame_rgba
    js.window.get_current_text = get_current_text
    js.window.get_current_effect_name = get_current_effect_name


def request_message(body):
    """Hand a new message body to the coordinator. Idempotent for duplicates."""
    if body is None:
        body = ""
    _coordinator.request_message(body)


def tick():
    """Advance the coordinator one frame. Call from the rAF loop."""
    _coordinator.tick()


def get_frame_rgba():
    """Return the current frame buffer as raw RGBA bytes for the JS blit."""
    return _web_canvas.to_imagedata()


def get_current_effect_name():
    """Return the class name of the active effect (status block)."""
    return _coordinator.current_effect_name


def get_current_text():
    """Return the body of the message currently being scrolled."""
    return _coordinator.current_text


# --- Install the JS surface AFTER the functions are defined. ---
_install_js_api()
