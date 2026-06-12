"""Browser entry point for the sign preview.

Runs in PyScript (Pyodide in the browser tab). Boots the WebCanvas /
WebDisplay, instantiates the effect cycle and scroller, wires up the
PreviewCoordinator, and exposes two JS-callable functions:

    coordinator.request_message(body) — hand a new body to the coordinator
    coordinator.tick()               — advance one frame

The actual main loop lives in static/preview.js, which drives tick() via
requestAnimationFrame (capped at 30 FPS) and calls request_message() from
its setInterval poll.

NOTE: no rgbmatrix import anywhere in this file or its imports (Pillow + numpy
are pulled in via the py-config.toml declared packages).
"""

import sys

# Make lib_shared, heart-matrix-controller, and heart-message-manager all
# importable. PyScript ships them under their declared URLs (see py-config.toml
# [files]); once fetched they live in the Pyodide FS and we add their
# containing dirs to sys.path so plain `import` works.
for path in ("/", "/heart-message-manager", "/heart-matrix-controller",
             "/heart-matrix-controller/patterns", "/lib_shared"):
    if path not in sys.path:
        sys.path.insert(0, path)

# Standard imports from the browser-side render path.
from preview_canvas import WebCanvas, WebDisplay
from preview_scroller import PreviewScroller
from preview_renderer import PreviewRenderer, PreviewCoordinator

# Pattern modules (no rgbmatrix, no OpenCV, no filesystem PNGs).
from patterns import fireworks, flame, nightsky, honeycomb  # noqa: F401
import patterns as patterns_module

# Lazy bundle the patterns module needs (the renderer looks classes up by
# name on the module).
import patterns  # noqa: F401  (already imported above, kept for clarity)

# The 64x64 logical panel — source of truth matches the device.
PANEL_WIDTH = 64
PANEL_HEIGHT = 64

# --- Build the coordinator ---

_web_canvas = WebCanvas(PANEL_WIDTH, PANEL_HEIGHT)
_display = WebDisplay(_web_canvas)

# PreviewRenderer skips PngDisplay / VideoDisplay; the others may also fail
# in the browser (e.g. Honeycomb's numpy import). Failures are logged + skipped.
_renderer = PreviewRenderer(_display, patterns_module)
_scroller = PreviewScroller(_display)
_coordinator = PreviewCoordinator(_display, _scroller, _renderer.effects, fade_seconds=4.0)


# --- JS-callable surface ---
#
# The JS main loop in static/preview.js calls these via the PyScript
# `pyscript` global. We bind them to module-level names so PyScript
# exposes them automatically.

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
