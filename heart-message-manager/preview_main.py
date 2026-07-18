"""Browser-side per-page binding shim for the sign preview.

The app-scoped `EffectsCoordinator` is created at PyScript load
time by `app_main.py` and exposed on `window._coordinator`. This
file runs after `app_main.py` and binds the coordinator to the
page-local render layer that the canvas needs to exist before
any frame can composite. The `MessageManager` is also app-scoped
(also constructed in `app_main.py`); the preview never creates
its own — the manager is the single source of truth for both
messages and config, and the coordinator's `on_change` callback
(registered in `app_main.py`) keeps the preview's pacing,
rotation, and scroller in sync.

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
  - `window.get_current_message`  — read the active Message dict
                                    (issue #38) — used to bind the
                                    preview status text to the
                                    Testing page's JSON modal.

The actual main loop lives in `static/preview/preview.js`, which
drives `tick()` via requestAnimationFrame (capped at 30 FPS). The
shared `MqttWsClient` (started by `app_main.py`) feeds
`window._message_manager.dispatch(raw)` on each inbound envelope;
the app-scoped MessageManager's universal `on_change` callback
calls `_coordinator.apply_settings(...)` and fans the change out
to JS subscribers via `App._dispatchChange`. The coordinator
itself pulls the next display message from the manager on a
250 ms throttle (see `EffectsCoordinator.get_display_message`).

The effect list, scroller color, and scroller speed all come
from the v2 `SignConfig` held by the app-scoped manager.
`build_effects` (in `lib_shared.effects_coordinator`) is the
single source of truth that translates an `EffectsSettings`
block into a list of instantiated Effect objects; both the
Pi and the preview call it. It delegates to
`lib_shared.effects_loader.make_effect_class` for the name →
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

from pyodide.ffi import create_proxy, to_js  # type: ignore[import-not-found]  (used by `_install_js_api`, `get_current_message`, `get_current_media`; Pyodide FFI for JS interop)
from pyodide_js import loadPackage  # type: ignore[reportGeneralTypeIssues]  # noqa: F401  (top-level await: PyScript 2024.9.x runs via `eval_code_async`)

print("[preview-py] module evaluation START (line 69)")
await loadPackage(["micropip", "numpy", "Pillow"])  # type: ignore[reportGeneralTypeIssues]  # top-level await — see note above
print("[preview-py] loadPackage complete (line 72)")

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
from lib_shared.models import EffectsSettings, TextSettings  # noqa: E402

print("[preview-py] preview_display/preview_scroller/effects_coordinator/models imported")

# Standard bitmap patterns the browser preview can run (no filesystem
# assets, no OpenCV). PngDisplay / VideoDisplay stay Pi-only; the
# shared `build_effects` factory filters them out by name.
from lib_shared.patterns.heartbeat import Heartbeat  # noqa: E402

print("[preview-py] heartbeat pattern imported")

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


def _get_app_coordinator():
    """Return the app-scoped `EffectsCoordinator` set by `app_main.py`.

    `app_main.py` runs as its own async PyScript task and assigns
    `window._coordinator`. By the time `_bootstrap()` calls this,
    the wait above has already confirmed the property is set.

    Renamed from `_coordinator` to avoid shadowing in PyScript's
    shared-globals namespace: `app_main.py` binds the bare name
    `_coordinator` to the EffectsCoordinator instance, so this
    function would get clobbered when `app_main.py` runs in the
    same globals dict — then `_coordinator()` would raise
    `'EffectsCoordinator' object is not callable`. Use the JS
    property (`js.window._coordinator`) as the source of truth
    instead.
    """
    coord = getattr(js.window, "_coordinator", None)
    if coord is None:
        raise RuntimeError("app_main.py did not install window._coordinator")
    return coord


# --- Build the page-local render layer (before awaiting the coordinator) ---

_web_canvas = WebCanvas(PANEL_WIDTH, PANEL_HEIGHT)
_display = WebDisplay(_web_canvas)
print(f"[preview-py] canvas built: {PANEL_WIDTH}x{PANEL_HEIGHT}")

# Boot-time defaults. The app-scoped MessageManager is the source of
# truth for the SignConfig; once the seed completes (called by app.js
# on page load), the manager's universal `on_change` callback fires
# `_coordinator.apply_settings(...)` and rebinds the rotation +
# scroller + pacing in place. Until that happens the canonical
# defaults (the same `EffectsSettings()` / `TextSettings()` the
# device boots with) are the visible state.
_settings = EffectsSettings()
_text_settings = TextSettings()
_effects = build_effects(_settings, display=_display)
_scroller = PreviewScroller(
    _display,
    color=_text_settings.color,
    speed=_text_settings.speed,
)
_heart = Heartbeat(_display)
print("[preview-py] heart effect built")


import asyncio  # noqa: E402


async def _wait_for_coordinator(timeout_s: float = 15.0, poll_ms: int = 20) -> None:
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
    print(f"[preview-py] _wait_for_coordinator START (timeout={timeout_s}s, poll={poll_ms}ms)")
    deadline = asyncio.get_event_loop().time() + timeout_s
    poll_s = poll_ms / 1000.0
    attempts = 0
    while True:
        coord = getattr(js.window, "_coordinator", None)
        if coord is not None:
            print(f"[preview-py] coordinator appeared after {attempts} polls ({attempts * poll_s:.2f}s)")
            return
        if asyncio.get_event_loop().time() >= deadline:
            has_app = getattr(js.window, "_message_manager", None) is not None
            has_seed = getattr(js.window, "_seed", None) is not None
            raise RuntimeError(
                f"app_main.py did not install window._coordinator within {timeout_s}s "
                f"({int(timeout_s / poll_s)} polls) — preview_main.py cannot bind. "
                f"_message_manager present={has_app}, _seed present={has_seed}"
            )
        attempts += 1
        if attempts % 25 == 0:  # every 500 ms
            print(f"[preview-py] still waiting for _coordinator after {attempts} polls ({attempts * poll_s:.2f}s)")
        await asyncio.sleep(poll_s)


async def _bootstrap() -> None:
    """Wait for `app_main.py`, then bind + start the preview.

    Everything that depends on the app-scoped coordinator lives
    inside this coroutine. The wait resolves the document-order
    race; the bind mirrors the device's startup sequence; the
    start kicks the boot splash. The coordinator's first pull
    (every 250 ms) produces the most recent message in the
    app-scoped manager's buffer.
    """
    await _wait_for_coordinator()
    print("[preview-py] _bootstrap: coordinator available, proceeding to bind")

    # --- Bind the render layer to the app-scoped coordinator ---
    # The coordinator is already wired to the app-scoped
    # MessageManager in `app_main.py` (constructor arg). The
    # preview's only job is to attach the page-local render
    # layer. The coordinator's first tick after `bind()` will
    # call `_sync_render_layer()` and read the manager's
    # current config into the rotation + scroller.
    coord = _get_app_coordinator()
    print(f"[preview-py] _bootstrap: got coordinator id={id(coord)}; calling coord.bind()")
    coord.bind(
        display=_display,
        scroller=_scroller,
        effects=_effects,
        heart=_heart,
    )
    print("[preview-py] _bootstrap: coord.bind() returned")
    _coord_ref["coord"] = coord

    # Begin the boot splash. The first pulled message (from the
    # app-scoped manager's buffer) plays once the heart fades out
    # — mirroring the device's "show the last seeded message at
    # startup" behavior.
    print("[preview-py] _bootstrap: calling coord.start()")
    coord.start()
    print("[preview-py] _bootstrap: coord.start() returned")

    # Install the JS surface last, once the coordinator is bound
    # and the boot has been kicked. Any `tick()` call that lands
    # after this returns is safe.
    _install_js_api()
    print("[preview-py] _bootstrap: complete; JS surface installed. py:done should fire.")


async def _bootstrap_with_logging():
    """Wrap `_bootstrap` so any exception is logged instead of swallowed.

    `asyncio.ensure_future` returns a Task that runs to completion
    even if it raises — but in PyScript 2024.9.x, an unhandled
    exception in a top-level task disappears from the devtools
    console. The whole reason the preview stops loading (and we
    can't tell why) is because the exception is invisible. This
    wrapper catches and prints.
    """
    try:
        await _bootstrap()
    except Exception as e:
        print(f"[preview-py] FATAL: _bootstrap raised {type(e).__name__}: {e}")
        import traceback

        traceback.print_exception(type(e), e, e.__traceback__)
        # Re-raise so the task is properly marked failed in
        # asyncio's eyes; PyScript's run mode surfaces it.
        raise


asyncio.ensure_future(_bootstrap_with_logging())


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
    js.window.get_current_media = get_current_media
    js.window.get_current_message = get_current_message
    js.window.get_diagnostics = get_diagnostics
    print(
        "[preview-py] _install_js_api: window.tick, get_frame_rgba, get_current_text, get_current_effect_name, get_current_media, get_current_message, get_diagnostics all installed"
    )


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


def get_current_message():
    """Return the full wire shape of the message currently being shown.

    Used by `preview.js` to bind the `#preview-message` text to a
    click handler that opens the same JSON modal the Testing page
    uses. The dict shape matches `Message.to_dict()` exactly
    (`id`, `sender`, `body`, `received_at`, `media`) so the
    serialized JSON in the modal is byte-identical to what the
    broker would publish — useful for confirming the coordinator
    picked up the same `media` list the wire carried.

    Returns `None` when the coordinator is idle (no picked entry —
    boot splash, SMS-only background, or post-`on_deck` consume).
    `preview.js` treats `None` as "no link target", so clicking
    the text in idle state is a no-op.

    Conversion note (issue #38 debug-2026-07-09): Pyodide's default
    conversion of a returned Python dict is a JS `Map`, not a plain
    `Object` — so `proxy.id` was always `undefined`, the link's
    `data-msg` was always deleted, and the click handler always
    fired the "no data-msg; idle state?" warning. We wrap the
    return in `to_js(..., dict_converter=Object.fromEntries)` so
    the JS side gets a real object whose property accessors work,
    `JSON.stringify` walks every field, and `btoa(...)` produces a
    matching base64 payload for the modal. The modal decode path
    stays as-is since it already works from the plain `Object`
    shape (`JSON.parse(decodeURIComponent(escape(atob(raw))))`).
    """
    coord = _coord()
    if coord is None:
        return None
    current = coord.current_message
    if current is None:
        return None
    raw = current.to_dict()
    return to_js(raw, dict_converter=js.Object.fromEntries)


def get_current_media():
    """Return the active MMS attachment the JS-side DOM should render.

    Read by `preview.js` on each animation frame. When the coordinator's
    current effect is a `BrowserMediaOverlay` (preview-side analogue of
    `MediaCycler`, issue #38), this returns a dict ``{url, kind, opacity,
    key}`` that drives the `<img>` / `<video>` element's `src` /
    visibility and `style.opacity`. Otherwise returns a stub with empty
    strings so the JS-side always gets a well-formed payload.

    The `key` is the bare S3 key; the JS uses it as the cache key
    for `<source>` element swapping (changing the URL on the same
    element doesn't always trigger a `load` event for video).

    Conversion note (issue #38 debug-2026-07-10): the dict is wrapped
    in `to_js(..., dict_converter=Object.fromEntries)` for the same
    reason `get_current_message` is — Pyodide's default conversion
    of a returned Python dict is a JS `Map`, not a plain `Object`,
    so `media.url` / `media.kind` / `media.key` were all `undefined`
    on the JS side. The diagnostic log
    `[preview-media] python returned empty: {key=undefined url=undefined
    kind=undefined opacity=undefined}` was the smoking gun — the
    overlay was producing a real URL but the JS never saw it. Pinning
    the `to_js` wrap here so the same bug class doesn't recur when
    a new field is added.
    """
    coord = _coord()
    if coord is None:
        return to_js(
            {"url": "", "kind": "", "opacity": 0.0, "brightness": 1.0, "key": ""},
            dict_converter=js.Object.fromEntries,
        )
    current = coord.current
    try:
        from lib_shared.patterns.browser_media_overlay import BrowserMediaOverlay
    except ImportError:
        return to_js(
            {"url": "", "kind": "", "opacity": 0.0, "brightness": 1.0, "key": ""},
            dict_converter=js.Object.fromEntries,
        )
    if isinstance(current, BrowserMediaOverlay):
        url = current.current_media_url
        kind = current.current_media_kind
        key = current.current_media_key
        opacity = current.current_opacity
        brightness = current.current_brightness
        # Source logging (issue #26 follow-up): the browser preview
        # has no Python-side fetch — the JS `<img>` / `<video>` element
        # does its own GET against `url`. The ground truth for "did
        # the image actually get requested" lives on the Flask side
        # (`/api/media/<key>` logs every 302 with `requester=browser`),
        # but it also helps to see here what URL we're handing back
        # to JS — if `url` is empty while `key` is set, something is
        # wrong in the overlay's `current_media_url` property
        # (probably `api_base_url` not bound). The log is throttled
        # so the 30 FPS rAF loop doesn't spam the console.
        if key:
            _preview_media_info(key, url, kind, opacity)
        return to_js(
            {
                "url": url,
                "kind": kind,
                "opacity": opacity,
                # `brightness` is the multiplicative boost applied
                # on top of full opacity (~1.15 by default). The JS
                # applies it as `style.filter = "brightness(N)"`
                # which scales pixel brightness multiplicatively —
                # matches the panel's channel-level clamping for
                # the Pi side. Sent UNCLAMPED; the JS clamps before
                # applying to keep the CSS sane.
                "brightness": brightness,
                "key": key,
            },
            dict_converter=js.Object.fromEntries,
        )
    # Throttled diagnostic: log when the current effect is NOT a
    # BrowserMediaOverlay but the current message has media — this
    # is the "cycler should be playing but isn't" path. The cycler
    # is the active effect during `in` and `hold`; everywhere else
    # the rotation effect is the healthy state, so the warning
    # would be a false positive.
    #
    # `coord.current_message` is the slot the `out→in` transition
    # populates from `on_deck`, so it's the authoritative read of
    # "what message is being staged." During `text_out` and
    # `background`, `current_message` is still the cycler's message
    # (the cycler has played; `_maybe_fall_back_to_rotation` swapped
    # `current` back to the rotation effect and armed the
    # suppress flag) — that's NOT a bug, it's the cycler-finished
    # state. Skip `text_out` and `background` so the diagnostic
    # only fires on the genuine "cycler should be active but
    # wasn't built" bug. `out` and `intro` are skipped for the
    # same reason: `current` is the previous rotation / heart
    # effect, the cycler hasn't been installed yet.
    try:
        current_msg = coord.current_message
        mode = getattr(coord, "mode", None)
        if current_msg is not None and (media := getattr(current_msg, "media", None) or []) and mode in ("in", "hold"):
            effect_name = type(current).__name__ if current is not None else "None"
            _preview_media_warn(
                "picked message has %d media item(s) but current effect is %s (not BrowserMediaOverlay); "
                "image will NOT render in preview",
                len(media),
                effect_name,
            )
    except Exception:
        pass
    return to_js(
        {"url": "", "kind": "", "opacity": 0.0, "brightness": 1.0, "key": ""},
        dict_converter=js.Object.fromEntries,
    )


def get_diagnostics():
    """Return a snapshot of coordinator state for browser-console diagnostics.

    Read by `preview.js` once per second so the developer console
    shows the live state machine values during a debug session. The
    fields captured are the ones that explain the most common
    "where did the text go?" bugs:

      - `mode`: one of `intro` / `out` / `in` / `hold` / `text_out`
        / `background`. The phases that visibly change the scroller's
        `set_brightness` ramp are `out` (1.0 → 0.0), `in` (0.0 → 1.0),
        and `text_out` (1.0 → 0.0 with the effect held).
      - `phase_elapsed`: seconds since the current `mode` started.
        Useful for correlating per-second logs against the
        configured `fade_seconds` / `hold_seconds` / `idle_seconds`.
      - `scroller_brightness`: the value `set_brightness` last
        applied to the scroller — this is what the canvas text pixels
        are actually being painted at. If this is 0 during a phase
        that should be lit, the coordinator's fade ramp is the bug,
        not the layer ordering.
      - `media_opacity`: the value `set_brightness` last applied to
        the active effect (BrowserMediaOverlay or rotation entry).
        When the picked message has MMS attachments and the
        overlay is the current effect, this is what controls the
        `<img>` / `<video>` element's CSS opacity (forwarded by
        `applyMedia` in preview.js).
      - `showing_text`: `True` when the scroller's body is non-empty
        and should be rendered. Goes `False` at the `text_out` →
        `background` transition; goes back to `True` at the next
        out → in transition.
      - `scroller_text`: the body currently being scrolled (truncated
        to 32 chars so the console line stays one-line per second).
      - `effect_name`: class name of `coord.current` — `BrowserMediaOverlay`
        while an MMS message is the picked entry, the rotation entry
        (e.g. `Fireworks`) once the overlay runs out of items or the
        buffer is empty.

    Returns the dict wrapped in `to_js(dict_converter=Object.fromEntries)`
    so the JS side gets a real `Object` (not a `Map`), and empty when
    the coordinator hasn't bound yet.
    """
    coord = _coord()
    if coord is None:
        return to_js({}, dict_converter=js.Object.fromEntries)
    current = coord.current
    effect_name = ""
    media_opacity = 0.0
    try:
        effect_name = type(current).__name__ if current is not None else ""
        from lib_shared.patterns.browser_media_overlay import BrowserMediaOverlay as _BMO

        if current is not None and isinstance(current, _BMO):
            media_opacity = float(current.current_opacity)
        elif current is not None and hasattr(current, "_brightness"):
            media_opacity = float(current._brightness)
    except (ImportError, AttributeError):
        pass
    scroller = coord.scroller
    scroller_text = scroller.text if scroller is not None else ""
    scroller_brightness = scroller._brightness if scroller is not None else 0.0
    fade_start = float(getattr(coord, "fade_start", 0.0) or 0.0)
    fade_seconds = 1.0
    try:
        fade_seconds = float(coord.message_manager.config.effects_settings.fade_seconds)
    except Exception:
        pass
    # Phase-elapsed: the coordinator only updates `phase_start` at the
    # `in→hold` transition (effects_coordinator.py:838), not at every
    # mode change. For the out / in / text_out / background phases
    # `phase_start` is stale — it carries over from the previous hold.
    # Use `fade_start` for the fade phases (out / in / text_out are all
    # fade ramps keyed off `fade_start`) and fall back to the live
    # `phase_start` only for hold / background.
    phase_elapsed = 0.0
    fade_progress = 1.0
    if coord.mode in ("out", "in", "text_out"):
        if fade_start > 0.0:
            phase_elapsed = _time.monotonic() - fade_start
            fade_progress = max(0.0, min(1.0, phase_elapsed / max(fade_seconds, 1e-6)))
    else:
        phase_start = float(getattr(coord, "phase_start", 0.0) or 0.0)
        if phase_start > 0.0:
            phase_elapsed = _time.monotonic() - phase_start
    return to_js(
        {
            "mode": coord.mode,
            "phase_elapsed": round(float(phase_elapsed), 2),
            "scroller_brightness": round(float(scroller_brightness), 3),
            "media_opacity": round(float(media_opacity), 3),
            "showing_text": bool(coord.showing_text),
            "scroller_text": (scroller_text or "")[:32],
            "effect_name": effect_name,
            "fade_progress": round(float(fade_progress), 3),
            "fade_seconds": round(float(fade_seconds), 2),
        },
        dict_converter=js.Object.fromEntries,
    )


# Throttled logger — fires at most once per second so the rAF loop
# at 30 FPS doesn't flood the browser devtools console. Tracks the
# last (effect_name, n_media) tuple it logged so a stable state
# produces zero output, but a state change emits one line.
import time as _time  # noqa: E402  (local import keeps module top tidy)

_preview_media_warn_last: dict = {"ts": 0.0, "key": None}
_preview_media_info_last: dict = {"ts": 0.0, "key": None}


def _preview_media_info(key: str, url: str, kind: str, opacity: float) -> None:
    """Throttled `console.log` for the browser-side source trace.

    Logs once per second per key (the S3 key is the stable identifier
    across cycles). The line shows what URL `BrowserMediaOverlay` is
    handing back to the JS `<img>` / `<video>` element on each frame,
    plus the opacity — when the operator reports "fade logs fire but
    no network call appears", this log + the Flask `/api/media/<key>`
    log together pin down whether the URL was constructed but never
    fetched (no Flask log, no Network tab request) or constructed and
    fetched but the response failed (Flask log shows the 302 but the
    `<img>`/`<video>` `error` event fires in the browser).
    """
    try:
        import js  # type: ignore[import-not-found]

        now = _time.monotonic()
        if key == _preview_media_info_last["key"] and now - _preview_media_info_last["ts"] < 1.0:
            return
        _preview_media_info_last["ts"] = now
        _preview_media_info_last["key"] = key
        source = "browser-proxy" if url else "<empty url — overlay not bound>"
        js.console.log(
            "[preview-media-source] key=%s source=%s url=%s kind=%s opacity=%.2f",
            key,
            source,
            url,
            kind,
            opacity,
        )
    except Exception:
        pass


def _preview_media_warn(fmt: str, *args: object) -> None:
    """Throttled `console.warn` for preview-media diagnostics.

    The 1-item case in `BrowserMediaOverlay` keeps `exhausted=False`
    forever, so a perfectly healthy state — current is a
    `BrowserMediaOverlay` — is silent. This helper only fires when
    something is wrong (a picked message has media but the current
    effect is not the overlay), and at most once per second per
    `(effect_name, n_media)` key.
    """
    try:
        import js  # type: ignore[import-not-found]

        effect_name = args[1] if len(args) > 1 else "?"
        n_media = args[0] if args else 0
        key = (effect_name, n_media)
        now = _time.monotonic()
        if key == _preview_media_warn_last["key"] and now - _preview_media_warn_last["ts"] < 1.0:
            return
        _preview_media_warn_last["ts"] = now
        _preview_media_warn_last["key"] = key
        js.console.warn("[preview-media] " + fmt % args)
    except Exception:
        # `js` may be unavailable in non-browser contexts; the test
        # suite imports this module via CPython and exercises the
        # return-value path, never the warning path. Silent skip.
        pass
