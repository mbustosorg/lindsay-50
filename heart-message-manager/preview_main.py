"""Browser-side per-page binding shim for the sign preview.

The app-scoped `EffectsCoordinator` is created at PyScript load
time by `app_main.py` and exposed on `window._coordinator`. This
file runs after `app_main.py` and binds the coordinator to the
page-local render layer that the canvas needs to exist before
any frame can composite.

PyScript 2024.9.x evaluates each `<py-script>` element as its
own async task — there is no guarantee they run in document
order, and `preview_main.py` has been observed to start before
`app_main.py` has finished assigning `window._coordinator`. The
waiter in `_wait_for_coordinator()` polls `js.window._coordinator`
for up to ~5 seconds (250 iterations × 20 ms) before giving up,
which covers PyScript's normal bootstrap latency.

Owns (per-page, not app-scoped):
  - the `WebCanvas` + `WebDisplay` (backed by the HTML5 `<canvas>`)
  - the `PreviewScroller` (Pillow text blit onto the WebCanvas)
  - the `Effects` rotation (built from the v2 `SignConfig`)
  - the `Heartbeat` boot-splash effect

Exposes the JS-callable surface that `static/preview/preview.js`
expects:
  - `window.tick`                 — advance the coordinator one frame
  - `window.get_frame_rgba`       — read the current frame buffer
  - `window.get_current_text`     — read the active message body
  - `window.get_current_effect_name` — read the active effect class

The actual main loop lives in `static/preview/preview.js`, which
drives `tick()` via requestAnimationFrame (capped at 30 FPS). The
shared `MqttWsClient` (started by `app_main.py`) feeds
`window._message_manager.dispatch(raw)` on each inbound envelope;
the MessageManager's universal `on_change` fan-out fires
`window.App._dispatchChange`. The per-page `MessageManager`
constructed here has its own `on_change` closure that calls
`coord.apply_settings(manager.config.effect_settings,
manager.config.text_settings)` and then fans the change out to
JS subscribers via `create_proxy(_on_change_js)()`. The
coordinator itself pulls the next display message from the
manager on a 250 ms throttle (see
`EffectsCoordinator.get_display_message`); no JS-driven push of
"next message body" or "apply config" is needed.

The effect list, scroller color, and scroller speed all come
from the v2 `SignConfig` exposed by `window._message_manager`.
`build_effects` (in `lib_shared.effects_coordinator`) is the
single source of truth that translates an `EffectsSettings`
block into a list of instantiated Effect objects; both the
Pi and the preview call it. It delegates to
`lib_shared.effects_factory.make_effect_class` for the name →
class mapping; that factory's per-name import scope means the
browser preview can request any effect by name and the
factory's import will only fail (return None) for the
Pi-only effects that need filesystem assets / OpenCV. The
shared builder falls back to the first canonical effect if
the rotation ends up empty (e.g. all effects disabled in the
admin UI), so the preview panel is never blank.

NOTE: no rgbmatrix import anywhere in this file or its
imports (Pillow + numpy are pulled in via the
py-config.toml declared packages).
"""

from pyodide_js import loadPackage  # type: ignore[reportGeneralTypeIssues]  # noqa: F401  (top-level await: PyScript 2024.9.x runs via `eval_code_async`)

await loadPackage(["micropip", "numpy", "Pillow"])  # type: ignore[reportGeneralTypeIssues]  # top-level await — see note above

import sys

# Make lib_shared, heart-matrix-controller, and heart-message-manager all
# importable. PyScript 2024.9.x's [files] handler writes each entry at
# the URL path (not the key), so the destination is the same as the
# URL we declared in py-config.toml. The PARENT of each package dir
# is what belongs in sys.path, so plain `import lib_shared` resolves
# as a package.
for path in (
    "/",
    "/static/preview",
    "/static/preview/heart-message-manager",
    "/static/preview/lib_shared",
):
    if path not in sys.path:
        sys.path.insert(0, path)

# Standard imports from the browser-side render path.
import js
from pyodide.ffi import create_proxy  # type: ignore[reportGeneralTypeIssues]

from preview_display import WebCanvas, WebDisplay  # noqa: E402
from preview_scroller import PreviewScroller  # noqa: E402
from lib_shared.effects_coordinator import build_effects  # noqa: E402
from lib_shared.message_manager import MessageManager  # noqa: E402
from lib_shared.models import EffectsSettings, TextSettings  # noqa: E402

# Standard bitmap patterns the browser preview can run (no filesystem
# assets, no OpenCV). PngDisplay / VideoDisplay stay Pi-only; the
# shared `build_effects` factory filters them out by name.
from lib_shared.patterns.heartbeat import Heartbeat  # noqa: E402

# The 64x64 logical panel — source of truth matches the device.
PANEL_WIDTH = 64
PANEL_HEIGHT = 64


# Module-level slot for the bound coordinator. `_bootstrap()`
# writes the app-scoped coordinator here once the wait resolves;
# the JS-callable surface reads from it. A dict (rather than
# the bare name `_coord`) makes the write/read split explicit
# and avoids the "local variable referenced before assignment"
# class of bug if a JS callback ever lands mid-bootstrap.
_coord_ref: dict = {}


def _coordinator():
    """Return the app-scoped `EffectsCoordinator` set by `app_main.py`.

    `app_main.py` runs as its own async PyScript task and assigns
    `window._coordinator`. By the time `_bootstrap()` calls this,
    the wait above has already confirmed the property is set.
    """
    coord = getattr(js.window, "_coordinator", None)
    if coord is None:
        raise RuntimeError("app_main.py did not install window._coordinator")
    return coord


# --- Build the page-local render layer (before awaiting the coordinator) ---

_web_canvas = WebCanvas(PANEL_WIDTH, PANEL_HEIGHT)
_display = WebDisplay(_web_canvas)

# Boot-time defaults. The MessageManager is the source of truth
# for the SignConfig; once the seed completes (called by app.js on
# page load), the manager's universal `on_change` closure fires
# `coord.apply_settings(...)` and rebinds the rotation + scroller
# + pacing in place. Until that happens the canonical defaults
# (the same `EffectsSettings()` / `TextSettings()` the device
# boots with) are the visible state.
_settings = EffectsSettings()
_text_settings = TextSettings()
_effects = build_effects(_settings, display=_display)
_scroller = PreviewScroller(
    _display,
    color=_text_settings.color,
    speed=_text_settings.speed,
)
_heart = Heartbeat(_display)


import asyncio  # noqa: E402


async def _wait_for_coordinator(timeout_s: float = 5.0, poll_ms: int = 20) -> None:
    """Poll `js.window._coordinator` until `app_main.py` sets it.

    PyScript 2024.9.x evaluates each `<py-script>` element as
    its own async task; there is no in-order guarantee. The
    preview's py-script element sits below the app's, but
    `app_main.py` is heavy (it imports `MessageManager`, builds
    the WS client, and posts a `loadPackage` await) — long
    enough that `preview_main.py` has been observed to reach
    its top-level statements first. This waiter closes that
    race without requiring a JS-side event hook.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    poll_s = poll_ms / 1000.0
    attempts = 0
    while True:
        if getattr(js.window, "_coordinator", None) is not None:
            if attempts > 0:
                print(f"[preview] coordinator appeared after {attempts} polls ({attempts * poll_s:.2f}s)")
            return
        if asyncio.get_event_loop().time() >= deadline:
            raise RuntimeError(
                f"app_main.py did not install window._coordinator within {timeout_s}s "
                f"({int(timeout_s / poll_s)} polls) — preview_main.py cannot bind"
            )
        attempts += 1
        await asyncio.sleep(poll_s)


async def _bootstrap() -> None:
    """Wait for `app_main.py`, then bind + start the preview.

    Everything that depends on the app-scoped coordinator /
    message manager lives inside this coroutine. The wait
    resolves the document-order race; the bind mirrors the
    device's startup sequence; the start kicks the boot splash.
    The coordinator's first pull (every 250 ms) produces the
    most recent message in the manager's buffer.
    """
    await _wait_for_coordinator()

    # --- Construct the per-page MessageManager with on_change closure ---
    # The closure applies the current config to the coordinator and fans
    # the change out to any JS subscribers registered via
    # `window.App.registerOnChange(...)`. The manager does NOT hold a
    # reference to the coordinator — the closure captures it.
    coord = _coordinator()

    def _on_change_js():
        """JS-side fan-out: tell every registered onChange listener
        that the manager's state mutated. Backed by `App._dispatchChange`
        in app.js, which iterates `App._onChangeListeners` and calls
        each callback with no args."""
        if hasattr(js.window, "App") and hasattr(js.window.App, "_dispatchChange"):
            js.window.App._dispatchChange()

    def _on_change():
        coord.apply_settings(manager.config.effect_settings, manager.config.text_settings)
        # Pyodide's `create_proxy` keeps a JS callback alive across
        # calls — a bare `_on_change_js` reference would be released
        # after this function returns and the JS dispatch would no-op.
        create_proxy(_on_change_js)()

    manager = MessageManager(
        on_change=_on_change,
        messages_api_url="",  # seeded by the app-scoped manager; not used here
        config_api_url="",
        api_key="",
    )
    # Replace the app-scoped reference so subsequent reads (status
    # block, etc.) reach this per-page manager that drives the
    # coordinator's on_change.
    js.window._message_manager = manager

    # --- Bind the render layer to the coordinator ---
    coord.bind(
        display=_display,
        scroller=_scroller,
        effects=_effects,
        heart=_heart,
    )
    _coord_ref["coord"] = coord

    # Begin the boot splash. The first pulled message (from the
    # manager's buffer, which is seeded by the app-scoped WS
    # client) plays once the heart fades out — mirroring the
    # device's "show the last seeded message at startup" behavior.
    coord.start()

    # Install the JS surface last, once the coordinator is bound
    # and the boot has been kicked. Any `tick()` call that lands
    # after this returns is safe.
    _install_js_api()
    print("[preview] bootstrap complete; JS surface installed")


asyncio.ensure_future(_bootstrap())


def _coord():
    """Return the bound coordinator, or None if bootstrap is still in flight.

    JS-callable surface (tick, get_current_text, ...) calls
    this on every invocation. The `None` branch is rare — those
    callbacks only run after `_install_js_api()` lands, which
    `_bootstrap()` only does after the coord is in the slot — but
    guarding makes the surface idempotent if a stray call sneaks in.
    """
    return _coord_ref.get("coord")


# --- JS-callable surface ---


# PyScript 2024.9.x removed the `window.pyscript.globals.get("name")`
# bridge that older releases used. The supported way to expose
# Python functions to JS is to assign them to `js.window` (the
# Pyodide proxy for the browser's `window` object) from within
# Python. The JS side then calls them as plain functions on
# `window` — no `pyscript` global involved.
def _install_js_api() -> None:
    js.window.tick = tick
    js.window.get_frame_rgba = get_frame_rgba
    js.window.get_current_text = get_current_text
    js.window.get_current_effect_name = get_current_effect_name


def tick():
    """Advance the coordinator one frame. Call from the rAF loop.

    No-op when the coordinator is unbound (defensive: the rAF
    loop in preview.js can fire before this file finishes
    evaluating if the user re-loads the page mid-bootstrap).
    """
    coord = _coord()
    if coord is not None:
        coord.tick()


def get_frame_rgba():
    """Return the current frame buffer as raw RGBA bytes for the JS blit."""
    return _web_canvas.to_imagedata()


def get_current_effect_name():
    """Return the class name of the active effect (status block)."""
    coord = _coord()
    return coord.current_effect_name if coord is not None else ""


def get_current_text():
    """Return the body of the message currently being scrolled."""
    coord = _coord()
    return coord.current_text if coord is not None else ""
