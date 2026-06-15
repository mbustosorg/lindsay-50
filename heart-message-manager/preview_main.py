"""Browser entry point for the sign preview.

Runs in PyScript (Pyodide in the browser tab). Boots the WebCanvas /
WebDisplay, instantiates the effect cycle and scroller, wires up the
shared EffectsCoordinator, and exposes JS-callable functions:

    request_message(body)        — hand a new body to the coordinator
    apply_config(cfg_dict)       — live-rebind effect rotation + scroller
    tick()                       — advance one frame
    get_frame_rgba()             — read the current frame buffer
    get_current_text()           — read the active message body
    get_current_effect_name()    — read the active effect class name

The actual main loop lives in static/preview.js, which drives tick() via
requestAnimationFrame (capped at 30 FPS) and calls request_message() from
the in-browser MessageManager's on_message signal. Config envelopes are
routed to `apply_config` so the preview rotation + scroller re-bind live
when the admin UI saves a new config.

The effect list, scroller color, and scroller speed all come from a
v2 `SignConfig` (the same shape `lib_shared.models` exposes to the
device). `build_effects` (in `lib_shared.effects_coordinator`) is
the single source of truth that translates an `EffectsSettings`
block into a list of instantiated Effect objects; both the Pi and
the preview call it. It delegates to
`lib_shared.effects_factory.make_effect_class` for the name → class
mapping; that factory's per-name import scope means the browser
preview can request any effect by name and the factory's import
will only fail (return None) for the Pi-only effects that need
filesystem assets / OpenCV — which the preview then filters out
by falling back to Fireworks.

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
from lib_shared.effects_coordinator import EffectsCoordinator, build_effects
from lib_shared.models import EffectsSettings, SignConfig, TextSettings

# Standard bitmap patterns the browser preview can run (no filesystem
# assets, no OpenCV). PngDisplay / VideoDisplay stay Pi-only.
from lib_shared.patterns.fireworks import Fireworks
from lib_shared.patterns.heartbeat import Heartbeat

# The 64x64 logical panel — source of truth matches the device.
PANEL_WIDTH = 64
PANEL_HEIGHT = 64

# --- Build the coordinator ---

_web_canvas = WebCanvas(PANEL_WIDTH, PANEL_HEIGHT)
_display = WebDisplay(_web_canvas)


def _build_preview_effects(settings: EffectsSettings) -> list:
    """Build the rotation list from the v2 EffectsSettings config.

    Delegates to `build_effects` (the shared orchestrator) which uses
    `lib_shared.effects_factory.make_effect_class` to resolve each
    enabled name. The factory's per-name import scope means PngDisplay
    and VideoDisplay are only imported if those effects are actually
    requested — the browser preview would fail their import (no
    OpenCV / filesystem assets in PyScript), so the factory returns
    None for them at runtime in the browser and the preview falls
    back to Fireworks so the panel never goes blank.
    """
    out = build_effects(settings, display=_display)
    if not out:
        # No browser-importable effects enabled in the config; show
        # at least one so the preview isn't a blank panel. Fireworks
        # is the most representative fallback (always-on, no asset
        # deps).
        out = [Fireworks(_display)]
    return out


# Module-level state. The boot path uses the canonical defaults (the
# admin UI would write the same shape); a live config envelope that
# arrives over MQTT/WS re-binds in place via `apply_config` below.
_settings = EffectsSettings()
_text_settings = TextSettings()
_effects = _build_preview_effects(_settings)
_scroller = PreviewScroller(
    _display,
    color=_text_settings.color,
    speed=_text_settings.speed,
)
_heart = Heartbeat(_display)
_coordinator = EffectsCoordinator(
    _display,
    _scroller,
    _effects,
    heart=_heart,
    settings=_settings,
)

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
    js.window.apply_config = apply_config
    js.window.get_frame_rgba = get_frame_rgba
    js.window.get_current_text = get_current_text
    js.window.get_current_effect_name = get_current_effect_name


def request_message(body):
    """Hand a new message body to the coordinator. Idempotent for duplicates."""
    if body is None:
        body = ""
    _coordinator.set_text(body)


def _js_to_dict(obj):
    """Convert a JsProxy (object) to a Python dict.

    Pyodide passes JS objects across as JsProxy. For config envelopes,
    we want a plain dict so SignConfig.from_dict can parse it. This is
    a shallow converter: nested dicts / lists of dicts are also converted.
    """
    d = dict(obj.to_py() if hasattr(obj, "to_py") else obj)
    return d


def apply_config(cfg_obj):
    """Live-rebind the preview from a config envelope.

    Called by preview.js when:
      - a config envelope arrives over MQTT/WS (live update from the
        admin UI saving new settings), or
      - the preview boots and `seedPreviewFromConfig` reads the
        current config from IndexedDB.

    Replaces the effects rotation in place, applies pacing + recent_count
    to the coordinator, and updates the scroller's color and speed.
    Idempotent: calling with the same cfg twice is a no-op for the
    scroller (set_color / set_speed overwrite with the same value) and
    cheap for the rotation (build_effects instantiates fresh Effect
    objects, but the coordinator's state machine will replace its
    `.effects` list reference and the next fade picks the head).
    """
    cfg_dict = _js_to_dict(cfg_obj)
    new_cfg = SignConfig.from_dict(cfg_dict)
    # Rebuild the rotation. _display is shared so the new effect instances
    # draw onto the same canvas the coordinator already composites to.
    _coordinator.effects = _build_preview_effects(new_cfg.effect_settings)
    _coordinator.idx = -1
    _coordinator.apply_settings(new_cfg.effect_settings)
    # Scroller live updates.
    ts = new_cfg.text_settings
    _scroller.set_color(ts.color)
    _scroller.set_speed(ts.speed)


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
