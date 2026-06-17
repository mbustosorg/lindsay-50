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
  - `window.request_message`      — hand a body to the coordinator
  - `window.apply_config`         — live-rebind rotation + scroller

The actual main loop lives in `static/preview/preview.js`, which
drives `tick()` via requestAnimationFrame (capped at 30 FPS). The
shared `MqttWsClient` (started by `app_main.py`) feeds
`window._message_manager.dispatch(raw)` on each inbound envelope;
the MessageManager's universal `on_change` fan-out fires
`window.App._dispatchChange`, and the preview's
`registerOnChange` listener (set up in preview.js) re-pushes the
current SignConfig and the most recent message into the
coordinator via `apply_config` and `request_message`.

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

from preview_display import WebCanvas, WebDisplay  # noqa: E402
from preview_scroller import PreviewScroller  # noqa: E402
from lib_shared.effects_coordinator import build_effects  # noqa: E402
from lib_shared.models import EffectsSettings, SignConfig, TextSettings  # noqa: E402

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


def _message_manager():
    """Return the app-scoped MessageManager (for `get_messages` / `get_config`).

    Same ordering concern as `_coordinator()` — `app_main.py`
    installs both. The boot waiter below blocks on
    `_coordinator` first, which `app_main.py` installs
    immediately after `_message_manager` in the same script, so
    by the time we get here `_message_manager` is set too.
    """
    mgr = getattr(js.window, "_message_manager", None)
    if mgr is None:
        raise RuntimeError("app_main.py did not install window._message_manager")
    return mgr


# --- Build the page-local render layer (before awaiting the coordinator) ---

_web_canvas = WebCanvas(PANEL_WIDTH, PANEL_HEIGHT)
_display = WebDisplay(_web_canvas)

# Boot-time defaults. The MessageManager is the source of truth
# for the SignConfig; once the seed completes (called by app.js on
# page load), the live config envelope fires `apply_config` and
# rebinds the rotation + scroller + pacing in place. Until that
# happens the canonical defaults (the same `EffectsSettings()` /
# `TextSettings()` the device boots with) are the visible state.
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
    device's startup sequence; the start kicks the boot splash
    with the latest seeded message (if any).
    """
    await _wait_for_coordinator()

    # --- Bind the render layer to the app-scoped coordinator ---
    coord = _coordinator()
    coord.bind(
        display=_display,
        scroller=_scroller,
        effects=_effects,
        heart=_heart,
    )
    _coord_ref["coord"] = coord

    # Begin the boot splash. The latest seeded message (if any) plays
    # once the heart fades out — mirroring the device's "show the last
    # seeded message at startup" behavior.
    try:
        entries = _message_manager().get_messages(limit=1, suppress=True)
    except Exception:
        entries = []
    startup_text = entries[0].message.body if entries else None
    coord.start(startup_text)

    # Install the JS surface last, once the coordinator is bound
    # and the boot has been kicked. Any `tick()` / `request_message`
    # / `apply_config` call that lands after this returns is safe.
    _install_js_api()
    print("[preview] bootstrap complete; JS surface installed")


asyncio.ensure_future(_bootstrap())


def _coord():
    """Return the bound coordinator, or None if bootstrap is still in flight.

    JS-callable surface (tick, apply_config, request_message) calls
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
    js.window.request_message = request_message
    js.window.apply_config = apply_config
    js.window.get_frame_rgba = get_frame_rgba
    js.window.get_current_text = get_current_text
    js.window.get_current_effect_name = get_current_effect_name


def request_message(body):
    """Hand a new message body to the coordinator. Idempotent for duplicates."""
    if body is None:
        body = ""
    coord = _coord()
    if coord is not None:
        coord.set_text(body)


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

    Called by preview.js when a config envelope arrives over the
    MQTT-WS connection (live update from the admin UI saving new
    settings). Replaces the effects rotation in place, applies
    pacing to the coordinator, and updates the scroller's color
    and speed. Idempotent: calling with the same cfg twice is a
    no-op for the scroller (set_color / set_speed overwrite with
    the same value) and cheap for the rotation (build_effects
    instantiates fresh Effect objects, but the coordinator's
    state machine will replace its `.effects` list reference and
    the next fade picks the head).
    """
    coord = _coord()
    if coord is None:
        print("[preview] apply_config called before bootstrap complete — ignoring")
        return
    try:
        cfg_dict = _js_to_dict(cfg_obj)
        new_cfg = SignConfig.from_dict(cfg_dict)
        # Rebuild the rotation. _display is shared so the new effect
        # instances draw onto the same canvas the coordinator already
        # composites to.
        new_effects = build_effects(new_cfg.effect_settings, display=_display)
        coord.effects = new_effects
        coord.idx = -1
        coord.apply_settings(new_cfg.effect_settings)
        # Scroller live updates.
        ts = new_cfg.text_settings
        _scroller.set_color(ts.color)
        _scroller.set_speed(ts.speed)
    except Exception as _exc:
        import traceback

        print(f"[preview] apply_config RAISED: {_exc!r}")
        traceback.print_exc()
        raise


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
