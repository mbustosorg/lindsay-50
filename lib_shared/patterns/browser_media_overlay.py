"""BrowserMediaOverlay — preview-side analogue of MediaCycler (issue #38).

The host-side `MediaCycler` (see `lib_shared.patterns.media_cycler`) decodes
each attachment with PIL/cv2 and blits it onto the rgbmatrix canvas through
the `Bitmap`/`Palette` pipeline. The browser preview cannot use that
pipeline — OpenCV isn't a Pyodide package, and pulling bytes into Pyodide
just to hand them back to the browser is busywork the browser already does
in hardware.

`BrowserMediaOverlay` is the lightweight alternative for the preview:
it carries the same per-message media list, picks an item with the same
cycle logic (D5/D12), and exposes the active attachment via three
read-only properties the JS-side `preview.js` reads each frame:

  - `current_media_url`: `f"{api_base_url}/api/media/{key}"` or `""` when exhausted.
    Resolved to the Flask proxy URL — the browser's native `<img>` /
    `<video>` follow the Flask 302 to the signed S3 URL the same way the
    Pi's `requests`-based fetcher does. Auth is the same Flask session
    cookie the `/preview` page already carries.
  - `current_media_kind`: `"image"` or `"video"`. Dispatched from the
    picked item's MIME type; `"video"` items play in `<video muted loop
    autoplay>` which the JS hides when the kind is `"image"`.
  - `current_opacity`: tracks the coordinator's `set_brightness(b)`
    ramp so the JS can fade the overlay in sync with the canvas's
    cross-fade. `1.0` when fully visible, `0.0` when faded out.

`render(canvas)` is a no-op (the DOM element is positioned over the canvas
via CSS — the LED-fuzzy canvas underneath continues to render the
rotation pattern). `tick()` is the cycle-advance clock.

The cycler keeps the same public surface as `MediaCycler`:
  - `message_id`, `media`
  - `exhausted: bool` — `True` when the list is empty; `False` for the
    1-item case (D12: the coordinator cuts off via `hold_seconds`,
    not via an internal "all shown" counter).
  - `set_brightness(b)`, `tick()`, `render(canvas)`.

This shape is what `EffectsCoordinator._maybe_build_media_cycler` selects
when the coordinator's `is_browser=True` flag is set. The Pi side keeps
constructing `MediaCycler` (its real PIL/cv2 path).
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

from lib_shared.effect_base import Effect

logger = logging.getLogger("heart")

# Per-item floor — matches MediaCycler's `_MIN_ITEM_SECONDS`. The
# 1-item case relies on this to keep `exhausted` False; the
# coordinator handles the cutoff via `hold_seconds`.
_MIN_ITEM_SECONDS = 10.0

# Multiplicative brightness boost applied on top of the coordinator's
# global ramp when forwarding `set_brightness(b)` to `self._brightness`.
# Mirrors MediaCycler's `_MEDIA_BRIGHTNESS_BOOST` — the two paths
# (Pi / browser preview) should produce visually equivalent brightness
# at the same coordinator `b`. On the preview side the JS-side
# `preview.js` reads `current_brightness` and applies it as a CSS
# `filter: brightness(N)` on the `<img>` / `<video>` overlay element,
# which scales pixel brightness multiplicatively (dark pixels pulled
# brighter, saturated pixels clamped by the browser's compositor) —
# matches the panel's behavior on the Pi (clamped at the 8-bit
# channel level inside `Effect.set_brightness`).
_BROWSER_MEDIA_BRIGHTNESS_BOOST = 1.15


class BrowserMediaOverlay(Effect):
    """Preview-side `Effect` that surfaces an MMS attachment to the DOM.

    See module docstring for the contract. Construction is cheap and
    side-effect free: the first call to `tick()` builds the active
    item (or flips `exhausted` on if every item is malformed). The
    JS-side `<img>` / `<video>` is updated by `preview.js` reading
    `current_media_url` + `current_media_kind` + `current_opacity`
    on every animation frame.

    Args:
        message_id: The inbound `Message.id` (for log context).
        media: The message's `media` list. Each entry is
            ``{"type": str, "url": str}`` (S3 key under our bucket).
        api_base_url: Origin of the Flask server
            (``str(js.window.location.origin)`` in the preview).
            Used to build the proxy URL the JS-side `<img>` / `<video>`
            calls. Empty string disables — every item is dropped at
            construction and `exhausted` flips on immediately.
        hold_seconds: The coordinator's configured message hold
            window. The overlay advances off the current item when
            `elapsed` exceeds `min(item_duration, hold_seconds)`;
            the coordinator's own clock handles the ultimate cut-off
            for multi-item lists.
    """

    def __init__(
        self,
        message_id: str,
        media: list[dict],
        *,
        api_base_url: str = "",
        hold_seconds: float = 15.0,
        brightness_boost: float = _BROWSER_MEDIA_BRIGHTNESS_BOOST,
    ) -> None:
        self.message_id = message_id
        self._api_base_url = (api_base_url or "").rstrip("/")
        self._hold_seconds = max(0.0, float(hold_seconds))
        # Multiplicative brightness boost forwarded alongside
        # `set_brightness`. Mirrors MediaCycler's
        # `_MEDIA_BRIGHTNESS_BOOST` so the Pi panel and the browser
        # preview produce visually equivalent brightness for the
        # same coordinator `b`. The JS-side `preview.js` reads
        # `current_brightness` and applies it as a CSS
        # `filter: brightness(N)` on the overlay `<img>` / `<video>`
        # element — matches the Pi's channel-level clamping inside
        # `Effect.set_brightness`.
        self._brightness_boost: float = float(brightness_boost)

        # `_items` carries one dict per attachment:
        #   {"type": str, "url": str, "key": str, "kind": str,
        #    "duration": float, "shown": bool, "ok": bool}
        # `ok` flips False on a malformed entry; `_cycle_advance`
        # skips ok=False items.
        self._items: list[dict] = []
        for entry in media or []:
            if not isinstance(entry, dict):
                continue
            mime = (entry.get("type") or "").lower()
            key = entry.get("url") or ""
            if not mime or not key:
                logger.warning(
                    "BrowserMediaOverlay: dropping malformed media entry id=%s mime=%r key=%r",
                    message_id,
                    mime,
                    key,
                )
                continue
            kind = "video" if mime.startswith("video/") else "image"
            self._items.append(
                {
                    "type": mime,
                    "key": key,
                    "kind": kind,
                    "duration": _MIN_ITEM_SECONDS,
                    "shown": False,
                    "ok": True,
                }
            )

        # Active item is the dict the JS-side DOM elements are
        # pointed at via `current_media_url`. `None` on construction
        # — populated on the first `tick()`.
        self._active: dict | None = None
        self._phase_start: float = time.monotonic()

        # Brightness ramp forwarded by the coordinator (mirrors the
        # `MediaCycler._brightness` field). Read-only from the JS
        # side via `current_opacity`.
        self._brightness: float = 1.0

        # `True` signals the coordinator to fall back to
        # `self.effects[self.idx]` on the next fade. For 1-item
        # lists this stays False forever (D12 — the coordinator
        # cuts off via `hold_seconds`).
        self.exhausted: bool = False

        # `True` signals the coordinator to fade out the cycler
        # and transition to the next rotation effect — mirrors
        # `MediaCycler.complete`. Distinct from `exhausted`:
        # `exhausted` means "I have no playable items left"
        # (D12 codec failure); `complete` means "I played
        # everything I was given — I'm done on purpose." The
        # coordinator's `_maybe_fall_back_to_rotation` checks
        # both via duck-typing (`getattr(current, "complete",
        # False)`) so the same fade-out trigger fires on the
        # preview side as on the Pi side.
        self.complete: bool = False

        if not self._items:
            self.exhausted = True
            logger.info(
                "BrowserMediaOverlay: empty at construction message_id=%s; signaling exhausted",
                message_id,
            )
        else:
            # Diagnostic — every active item's S3 key is logged at
            # INFO so the browser devtools / Flask logs show what
            # the overlay picked up from the wire. The keys are
            # not sensitive (S3 keys under our own bucket) and the
            # log fires once per message transition, not per frame.
            keys = [it["key"] for it in self._items]
            logger.info(
                "BrowserMediaOverlay: constructed message_id=%s items=%d api_base_url=%r keys=%s",
                message_id,
                len(self._items),
                self._api_base_url,
                keys,
            )

    # -- read-only surface (consumed by preview.js) ------------------------

    @property
    def current_media_url(self) -> str:
        """Flask proxy URL for the active item, or ``""`` when idle."""
        if self.exhausted or self._active is None or not self._api_base_url:
            return ""
        return f"{self._api_base_url}/api/media/{self._active['key']}"

    @property
    def current_media_key(self) -> str:
        """The S3 key of the active item, or ``""`` when idle."""
        if self.exhausted or self._active is None:
            return ""
        return self._active["key"]

    @property
    def current_media_kind(self) -> str:
        """``"image"`` or ``"video"`` — the DOM element the JS should drive."""
        if self.exhausted or self._active is None:
            return ""
        return self._active["kind"]

    @property
    def current_opacity(self) -> float:
        """Coordinator's fade ramp, clamped to [0, 1] for CSS opacity.

        Distinct from `current_brightness` — opacity drives the
        fade-in / fade-out crossfade (0 = invisible, 1 = visible);
        brightness is the multiplicative boost on top of full
        opacity. The two are applied independently in `preview.js`
        via `style.opacity` and `style.filter = "brightness(N)"`.
        """
        return max(0.0, min(1.0, float(self._brightness)))

    @property
    def current_brightness(self) -> float:
        """The multiplicative brightness boost applied to the overlay.

        Returned as the raw boosted value (NOT clamped to 1.0) so
        the JS-side `filter: brightness(N)` can render the ~15%
        boost on top of full opacity. `preview.js` clamps the
        filter value to a sane range before applying it to the
        DOM, but the panel's effect on the Pi is visually
        equivalent: dark pixels pulled brighter, saturated pixels
        held at the 8-bit ceiling.

        Mirrors `MediaCycler`'s `_brightness_boost` factor — both
        paths produce visually equivalent brightness for the same
        coordinator `b`.
        """
        return float(self._brightness) * self._brightness_boost

    @property
    def items_remaining(self) -> int:
        """Count of items still in the working list (after malformed drops)."""
        return len(self._items)

    # -- Effect interface ----------------------------------------------------

    def set_brightness(self, b: float) -> None:
        """Forward the coordinator's global brightness.

        The browser overlay reads `current_opacity` (the clamped
        fade-ramp value) and `current_brightness` (the boosted
        value, unclamped) on every frame and applies them to the
        `<img>` / `<video>` element's `style.opacity` and
        `style.filter` respectively. There is no per-pixel fade
        — the browser's compositor handles that for free — but
        the values still have to track the coordinator's cross-
        fade so the underlying canvas + overlay line up.
        """
        self._brightness = max(0.0, float(b))

    def tick(self) -> None:
        """Advance the cycle clock.

        First tick: pick the first item. Subsequent ticks: when the
        active item's `duration` (10s floor) elapses, advance to a
        fresh item. Honors `hold_seconds` by stopping the advance
        when the cumulative window exceeds the hold — the
        coordinator's own clock handles the ultimate cut-off.

        For the 1-item case, mirrors `MediaCycler.tick`: flip
        `complete=True` after `item["duration"]` seconds so the
        coordinator fades the overlay out and transitions to a
        rotation effect (issue #38 follow-up — the prior behavior
        looped the same frame for the full hold / idle window).
        """
        if self.exhausted:
            return
        if self._active is None:
            self._cycle_advance(initial=True)
            return

        elapsed = time.monotonic() - self._phase_start
        if len(self._items) <= 1:
            # 1-item case — never advance internally, but DO flip
            # `complete` after the item's natural duration so the
            # coordinator swaps us out for the rotation effect
            # instead of looping the same frame for the full
            # hold / idle window.
            if len(self._items) == 1:
                if elapsed >= self._items[0]["duration"]:
                    self.complete = True
            return
        if elapsed >= self._active["duration"]:
            if elapsed >= self._hold_seconds:
                # Honor the hold cutoff; the coordinator transitions
                # on its own clock.
                return
            self._cycle_advance()

    def render(self, canvas: Any) -> None:
        """No-op for the overlay path — the DOM element is the renderer.

        The effect base class's default `render` clears the canvas
        and re-blits any preset bitmap/palette. Calling that here
        would clobber the LED-fuzzy canvas the preview's render
        loop just blitted. The overlay intentionally leaves the
        canvas alone so the rotation pattern shows through; the
        `<img>` / `<video>` element above the canvas is the
        visible "rendered" attachment.
        """
        del canvas  # intentionally unused — see docstring

    # -- cycle advance -------------------------------------------------------

    def _cycle_advance(self, *, initial: bool = False) -> None:
        """Pick the next item to render.

        Mirrors `MediaCycler._cycle_advance` so the coordinator's
        behavior is identical across host + preview. Items are
        selected uniformly at random from not-yet-shown-this-cycle;
        the cycle resets when every item has been shown. The 1-item
        case never auto-advances — `hold_seconds` drives the
        cut-off.
        """
        if not self._items:
            self.exhausted = True
            self._active = None
            return

        if len(self._items) == 1:
            self._active = self._items[0]
            self._phase_start = time.monotonic()
            if initial:
                self._active["shown"] = True
            return

        not_shown = [it for it in self._items if not it["shown"]]
        all_shown = not not_shown
        candidates = not_shown if not_shown else self._items
        chosen = random.choice(candidates)
        self._active = chosen
        self._phase_start = time.monotonic()
        chosen["shown"] = True
        # Once every item has been shown, the cycle is done — flip
        # `complete` so the coordinator falls back to the rotation
        # effect at the next tick. We don't reset the `shown` flags;
        # if the coordinator wants another cycle, a fresh overlay
        # will be constructed at the next out→in.
        if all_shown:
            self.complete = True
        if not initial:
            # Initial picks fire on the first `tick()`; logging them
            # is noisy because every overlay construction triggers
            # one. The cycle-advance path is what we care about.
            logger.info(
                "BrowserMediaOverlay: cycled to key=%s kind=%s message_id=%s",
                chosen["key"],
                chosen["kind"],
                self.message_id,
            )
