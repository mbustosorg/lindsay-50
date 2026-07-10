"""MediaCycler — per-message background effect for MMS messages (issue #38).

`MediaCycler` is the bridge between an inbound MMS `Message.media` list
and the panel render loop. The coordinator constructs one when a
message with non-empty `media` lands on the scroller; the cycler takes
over from `self.effects[self.idx]` for the duration of the message's
hold. When the cycle ends (all items shown) or `hold_seconds` elapses
(coordinator's existing cutoff), the coordinator falls back to
`self.effects[self.idx]`.

Design references (see openspec `add-image-and-video-support`):
  D4 — per-message Effect, not a rotation entry
  D5 — cycle window = `max(10s, media_duration)`, cut off at `hold_seconds`
  D7 — direct imports of inner renderers (not via the effects loader)
  D12 — codec-failure handling: drop bad items, fall back to rotation
       when the list becomes empty

Inner-renderer dispatch (D7):
  - `image/*` mime types → `ImageDisplay` (palette pipeline, from
    `lib_shared.patterns.image_display`)
  - `video/*` mime types → `VideoDisplay` (full-frame SetImage
    pipeline, from `lib_shared.patterns.video_display`)

These classes are NOT in the effects registry (D6) — the registry
carries only the 5 non-media effects. The cycler imports them
directly so a stale operator override that mentions `ImageDisplay`
or `VideoDisplay` cannot brick the rotation path.

Codec-failure handling (D12):
  - The first frame of an inner renderer is read inside `cycle_advance`.
  - On `cv2.error`, `PIL.UnidentifiedImageError`, `OSError`, or any
    decode-related exception, the offending item is dropped from
    `self._items` and the next advance picks a fresh one.
  - If `self._items` becomes empty, `exhausted` is True — the
    coordinator uses that signal to fall back to
    `self.effects[self.idx]` on the next fade.
  - For the 1-item case, `exhausted` stays False — the coordinator
    handles the cutoff via `hold_seconds` (no internal "all items
    shown" counter). Spec wording: "the cycler's 'advance or hold'
    clock is gated by hold_seconds, not by an internal 'all items
    shown' counter."

URL → local file path:
  Each item's `media[*].url` is a logical S3 key. The cycler resolves
  it to a Flask proxy URL (`{api_base_url}/api/media/{key}`), fetches
  the bytes (the Flask 302 redirects to a signed S3 URL), writes
  them to a local cache file, and constructs the inner renderer with
  that local path. The cache is keyed by S3 key, so re-entries of the
  same key reuse the same file. The fetcher is injectable (constructor
  kwarg) so tests can run without HTTP — host-side tests don't have
  the network or the S3 client.
"""

import logging
import os
import random
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from lib_shared.display_base import DisplayBase
from lib_shared.effect_base import Effect

logger = logging.getLogger("heart")

# Floor for the per-item display window — D5: `max(10s, source_duration)`.
# Image items don't have a natural duration, so the floor wins.
_MIN_ITEM_SECONDS = 10.0

# Multiplicative brightness boost applied on top of the coordinator's
# global ramp when forwarding `set_brightness(b)` to the active inner
# renderer (ImageDisplay / VideoDisplay). 1.15 = ~15% brighter than the
# rotation effects, which compensates for the loss of perceived
# brightness that photos and videos carry vs. generated pixel art —
# night-sky stars and flame highlights look more vivid on the panel
# than a luminance-balanced JPEG does at the same nominal brightness.
#
# This is a behavioral knob of the cycler's rendering algorithm, NOT
# a per-deployment operational value — it lives in code (not
# settings.toml) per the project's "behavioral knobs in code" rule.
# Surfaces as a settings.toml knob later if operators ask to tune it.
#
# Applied unclamped at the cycler level: the inner renderer clamps at
# the channel level (255 for 8-bit palette entries, 255 for 8-bit
# pixel values) so dark pixels get pulled brighter (int(100*1.15)=115)
# while already-saturated pixels stay clamped (int(255*1.15)=min(255,293)=255).
# The visible effect: media appears consistently brighter than the
# rotation effects at the same nominal brightness.
_MEDIA_BRIGHTNESS_BOOST = 1.15


class MediaCycler(Effect):
    """Effect that cycles through a message's media attachments.

    Per-message construction: the EffectsCoordinator instantiates one
    `MediaCycler` at the `out → in` transition when the message about
    to be shown has a non-empty `media` list, then assigns it to
    `self.current` in place of `self.effects[self.idx]`. The cycler
    cycles through the items uniformly at random (re-rolling for not-
    yet-shown items first), cuts off at `hold_seconds`, and yields
    back to the rotation when its in-memory list runs out.

    Public surface is the Effect interface (`tick`, `render`,
    `set_brightness`) — the coordinator's `self.current` swap
    is identical to swapping to any other Effect. The cycler also
    exposes `exhausted` so the coordinator can decide on the next
    fade whether to use the cycler or fall back to the rotation.
    """

    def __init__(
        self,
        message_id: str,
        media: list[dict],
        *,
        display: DisplayBase,
        api_base_url: str = "",
        hold_seconds: float = 15.0,
        cache_dir: str | Path | None = None,
        fetcher: Optional[Callable[[str], bytes]] = None,
        api_key: str = "",
        brightness_boost: float = _MEDIA_BRIGHTNESS_BOOST,
    ) -> None:
        """Initialize the cycler.

        Args:
            message_id: Id of the inbound message (for log context only).
            media: `Message.media` list — each entry is
                ``{"type": str, "url": str}`` (S3 key).
            display: DisplayBase whose `canvas.width` / `canvas.height`
                drive the inner renderer geometry.
            api_base_url: Origin of the Flask server (e.g.
                ``http://localhost:3100``). The cycler builds
                ``{api_base_url}/api/media/{key}`` URLs. Empty string
                disables fetching and the cycler logs a WARNING for
                each item (used in tests + the offline pre-cache path).
            hold_seconds: Seconds the coordinator holds the message.
                The cycler cuts off mid-list when this elapses. The
                coordinator handles the actual transition out of
                `hold` mode — this value is consulted by the
                cycler's own "advance" decisions only.
            cache_dir: Local directory for downloaded media bytes. None
                uses the OS temp dir. Reused across cycles for the
                same S3 key.
            fetcher: Callable `(url: str) -> bytes` for HTTP fetch.
                Defaults to a `requests.get` wrapper that follows
                redirects. None is equivalent to passing the default.
                Caller-supplied fetchers are NOT given the api_key —
                they're expected to manage their own auth (tests +
                the offline pre-cache path that pulls bytes from S3
                directly without going through Flask).
            api_key: X-API-Key value to send on the default fetcher's
                GET to Flask's `/api/media/<key>` route. Flask gates
                that route with `@api_login_required`, which checks
                the `X-API-Key` header before falling through to the
                browser session — the Pi has no session cookie, so
                without this header every fetch 401s and the cycler
                drops the item (D12 codec-failure semantics). Same
                value as `cfg.API_SECRET_KEY` on the Flask server.
                Empty string disables the header (test/offline path).
        """
        self.message_id = message_id
        self._media = [m for m in (media or []) if isinstance(m, dict)]
        self._display = display
        self._api_base_url = (api_base_url or "").rstrip("/")
        self._hold_seconds = max(0.0, float(hold_seconds))
        self._cache_dir = Path(cache_dir) if cache_dir is not None else Path(tempfile.gettempdir()) / "lindsay-50-media"
        # Bind the api_key into the default fetcher so every fetch to
        # Flask carries `X-API-Key`. Caller-supplied fetchers are left
        # untouched — they're test doubles or pre-cache paths that own
        # their own auth.
        self._fetcher = fetcher or _build_default_fetcher(api_key)

        # `_items` is the mutable working list: each entry is
        # `{"type": str, "url": str, "shown": bool, "path": str | None,
        #  "duration": float, "renderer": Effect | None}`. Items get
        # popped on codec failure (D12).
        self._items: list[dict] = []
        for entry in self._media:
            mime = (entry.get("type") or "").lower()
            key = entry.get("url") or ""
            if not mime or not key:
                logger.warning(
                    "MediaCycler: dropping malformed media entry id=%s mime=%r key=%r",
                    message_id,
                    mime,
                    key,
                )
                continue
            self._items.append(
                {
                    "type": mime,
                    "url": key,
                    "shown": False,
                    "path": None,
                    "duration": _MIN_ITEM_SECONDS,
                    "renderer": None,
                }
            )

        # Active renderer — the inner Effect whose `tick` / `render`
        # are called by the coordinator's composite step.
        self._active: Effect | None = None
        # Brightness from the coordinator (forwarded to the active
        # renderer; not applied independently — D7: "the cycler's
        # `set_brightness` is stored as a factor and applied when
        # blitting"). The cycler multiplies this by `_brightness_boost`
        # before forwarding to the active renderer so media appears
        # consistently brighter than the rotation effects at the
        # same nominal coordinator brightness.
        self._brightness: float = 1.0
        # Multiplicative boost forwarded alongside `set_brightness`.
        # Module-level constant by default; constructor kwarg lets
        # callers (tests) override it.
        self._brightness_boost: float = float(brightness_boost)
        # Phase tracking for advance — `hold` keeps the active item
        # until its window elapses, `advance` swaps to a new item on
        # the next tick. We don't track `out` / `in` internally; the
        # coordinator's global brightness ramp handles the cross-fade.
        self._phase: str = "hold"
        self._phase_start: float = time.monotonic()
        # When True, the cycler has nothing left to render — the
        # coordinator should fall back to `self.effects[self.idx]`
        # on the next fade. For a single-item list this stays False
        # forever (D5/D12: the cycler's "advance or hold" clock is
        # gated by `hold_seconds`, not by an internal "all items
        # shown" counter).
        self.exhausted: bool = False
        # When True, the cycler has shown all of its natural content
        # and the coordinator should fall back to the rotation effect
        # at the next tick. Distinct from `exhausted` (D12 codec
        # failure): `exhausted` means "I have no playable items left
        # to try"; `complete` means "I played everything I was given
        # — I'm done on purpose." For 1-item lists, `complete` flips
        # after `item["duration"]` seconds (10s for images, video
        # length for videos). For multi-item, after every item has
        # been shown at least once. Coordinator swap-in happens via
        # `_maybe_fall_back_to_rotation` (called from `hold` and
        # `background` branches) — the same duck-typed check that
        # already handles `exhausted`.
        self.complete: bool = False

        if self._items:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            self._cycle_advance(initial=True)
        else:
            # All items dropped at construction (malformed entries).
            # The coordinator falls back immediately.
            self.exhausted = True
            logger.info(
                "MediaCycler: empty at construction message_id=%s; signaling exhausted",
                message_id,
            )

    # -- item resolution ------------------------------------------------------

    def _resolve_local_path(self, item: dict) -> str | None:
        """Resolve `item["url"]` (S3 key) to a local file path.

        Caches across cycles: same S3 key returns the same file. On
        fetch failure the item is dropped (D12). Tests pass a `fetcher`
        callable that returns valid bytes synchronously, so the host
        test suite runs without HTTP.
        """
        # If the item has a pre-stashed path (test seam, or set by
        # a previous successful resolve), reuse it. This also means
        # tests can pre-populate the cache and the cycler never needs
        # to call the fetcher.
        stashed = item.get("path")
        if stashed and Path(stashed).exists() and Path(stashed).stat().st_size > 0:
            return stashed

        key = item["url"]
        # Cache by a sha-ish filename derived from the S3 key (the
        # key is already unique per Twilio upload — sha256 is overkill
        # but cheap and gives us a stable, URL-safe name).
        import hashlib

        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        # Preserve the extension so `cv2.VideoCapture` sniffs it
        # without depending on the file content alone.
        ext = os.path.splitext(key)[1] or _ext_for_mime(item["type"])
        cached = self._cache_dir / f"{digest}{ext}"
        if cached.exists() and cached.stat().st_size > 0:
            return str(cached)

        if not self._api_base_url:
            # No Flask URL — can't fetch. Drop the item (D12: codec
            # failure semantics — we never even got to attempt decode).
            logger.warning(
                "MediaCycler: cannot fetch %s (no api_base_url); dropping item",
                key,
            )
            return None

        proxy_url = f"{self._api_base_url}/api/media/{key}"
        try:
            data = self._fetcher(proxy_url)
        except Exception as exc:  # noqa: BLE001 — fetcher can raise anything
            logger.warning(
                "MediaCycler: fetch failed key=%s err=%s; dropping item",
                key,
                exc,
            )
            return None
        if not data:
            logger.warning("MediaCycler: empty body for key=%s; dropping item", key)
            return None
        try:
            cached.write_bytes(data)
        except OSError as exc:
            logger.warning(
                "MediaCycler: cache write failed key=%s path=%s err=%s; dropping item",
                key,
                cached,
                exc,
            )
            return None
        return str(cached)

    def _read_duration(self, item: dict, local_path: str) -> float:
        """Compute the item's natural display window in seconds.

        For videos, reads `cv2.CAP_PROP_FRAME_COUNT` /
        `cv2.CAP_PROP_FPS` once and caches it on the item. For images,
        returns `_MIN_ITEM_SECONDS` (no natural duration; the floor
        wins). On read failure (codec missing, corrupt file) returns
        the floor — D12 lets the inner renderer raise the actual
        decode error on first frame.
        """
        mime = item["type"]
        if mime.startswith("video/"):
            try:
                import cv2  # type: ignore[import-not-found]

                cap = cv2.VideoCapture(local_path)
                if not cap.isOpened():
                    cap.release()
                    return _MIN_ITEM_SECONDS
                fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
                frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
                cap.release()
                if fps > 0 and frame_count > 0:
                    return max(_MIN_ITEM_SECONDS, float(frame_count) / float(fps))
            except Exception as exc:  # noqa: BLE001 — cv2 / OS can raise anything
                logger.debug(
                    "MediaCycler: duration read failed for %s: %s; using floor",
                    local_path,
                    exc,
                )
        return _MIN_ITEM_SECONDS

    def _build_inner(self, item: dict) -> Effect | None:
        """Construct an inner renderer for `item`. Returns None on
        codec failure / fetch failure (D12: caller drops the item)."""
        local_path = self._resolve_local_path(item)
        if not local_path:
            return None
        item["path"] = local_path
        item["duration"] = self._read_duration(item, local_path)
        mime = item["type"]
        try:
            if mime.startswith("image/"):
                from lib_shared.patterns.image_display import ImageDisplay

                return ImageDisplay(self._display, path=local_path)
            if mime.startswith("video/"):
                from lib_shared.patterns.video_display import VideoDisplay

                return VideoDisplay(self._display, path=local_path)
        except Exception as exc:  # noqa: BLE001 — PIL/cv2 raise on corrupt bytes
            logger.warning(
                "MediaCycler: dropping item %r due to decode failure: %s",
                item["url"],
                exc,
            )
            # Cleanup any half-built renderer's open resources.
            renderer = item.get("renderer")
            if renderer is not None and hasattr(renderer, "close"):
                try:
                    renderer.close()
                except Exception:  # noqa: BLE001
                    pass
            return None
        logger.warning(
            "MediaCycler: dropping item %r: unsupported mime type %r",
            item["url"],
            mime,
        )
        return None

    # -- cycle advance --------------------------------------------------------

    def _cycle_advance(self, *, initial: bool = False) -> None:
        """Pick the next item to render. Drops items that fail to build.

        For 1-item lists, the same item is re-used indefinitely (the
        cycler's "advance or hold" clock is gated by hold_seconds, not
        by an internal "all items shown" counter — D5 / D12).

        Updates `self._active`, `self.exhausted`, and the per-item
        `shown` flag. When every item is shown OR every item has been
        dropped, `exhausted` becomes True and the coordinator falls
        back to the rotation on the next fade.
        """
        if not self._items:
            self.exhausted = True
            self._active = None
            return

        # 1-item case: never advance internally — the coordinator
        # cuts off via hold_seconds. Re-pick the same item. Flip
        # `complete` after the item's natural duration so the
        # coordinator swaps us out for the rotation effect (instead
        # of looping the same frame for `idle_seconds`).
        if len(self._items) == 1:
            item = self._items[0]
            if item["renderer"] is None:
                renderer = self._build_inner(item)
                if renderer is None:
                    self._drop_item(item)
                    self._active = None
                    self.exhausted = True
                    return
                item["renderer"] = renderer
            self._active = item["renderer"]
            self._phase = "hold"
            self._phase_start = time.monotonic()
            # Mark as shown once the first frame has been rendered
            # (the actual `shown` flip happens after the first
            # `tick` succeeds — see `tick`).
            if initial:
                item["shown"] = True
            return

        # Multi-item case: pick uniformly at random from
        # not-yet-shown-this-cycle items. If every item is shown,
        # we've completed the cycle — flip `complete` so the
        # coordinator swaps us out for the rotation effect instead
        # of looping the same item set forever.
        not_shown = [it for it in self._items if not it["shown"]]
        all_shown = not not_shown
        candidates = not_shown if not_shown else self._items
        chosen = random.choice(candidates)

        # Build the inner renderer. On failure (D12), drop the item
        # and recurse. The recursion is bounded by len(self._items) —
        # if every item fails, the recursion bottoms out and
        # `exhausted` flips on.
        if chosen["renderer"] is None:
            renderer = self._build_inner(chosen)
            if renderer is None:
                self._drop_item(chosen)
                return self._cycle_advance(initial=initial)
            chosen["renderer"] = renderer
        self._active = chosen["renderer"]
        self._phase = "hold"
        self._phase_start = time.monotonic()
        chosen["shown"] = True
        # Once every item has been shown, the cycle is done — flip
        # `complete` so the coordinator falls back to the rotation
        # effect at the next tick. We don't reset the `shown` flags;
        # if the coordinator wants another cycle (e.g. the
        # rotation effect also finished early), a fresh MediaCycler
        # will be constructed at the next out→in.
        if all_shown:
            self.complete = True

    def _drop_item(self, item: dict) -> None:
        """Remove `item` from `self._items` and clean up its renderer."""
        if item in self._items:
            self._items.remove(item)
        renderer = item.get("renderer")
        if renderer is not None and hasattr(renderer, "close"):
            try:
                renderer.close()
            except Exception:  # noqa: BLE001
                pass
        logger.warning(
            "MediaCycler: dropping item %r due to decode failure",
            item.get("url"),
        )

    # -- Effect interface -----------------------------------------------------

    def set_brightness(self, b: float) -> None:
        """Forward the coordinator's global brightness to the active renderer.

        Both ImageDisplay and VideoDisplay accept the standard
        `set_brightness(b)` call (D7: "the cycler's `set_brightness`
        is stored as a factor and applied when blitting" — applies
        to VideoDisplay; ImageDisplay multiplies it with its own
        per-image fade level).

        The cycler applies a multiplicative `_brightness_boost` (see
        module-level `_MEDIA_BRIGHTNESS_BOOST`) on top of `b` before
        forwarding. The boost is sent UNCLAMPED — inner renderers
        clamp at the channel level (8-bit palette entries / 8-bit
        pixel values), which is the right grain for "push dark
        pixels brighter, leave saturated pixels saturated." A 1.15×
        boost on a 100/255 channel becomes 115/255 (visible
        brightening); a 1.15× boost on a 255/255 channel stays at
        255 (clamped).
        """
        self._brightness = b
        if self._active is not None:
            self._active.set_brightness(b * self._brightness_boost)

    def tick(self) -> None:
        """Advance the cycler one frame.

        For the 1-item case, advances the active renderer's frame
        loop. For multi-item, also checks the per-item window — when
        `elapsed >= item["duration"]`, marks the current item as
        fully shown and picks the next on the next tick.

        Codec failures inside the active renderer propagate here as
        decode exceptions. We catch and drop the item (D12); if the
        list becomes empty, `exhausted` flips on.
        """
        if self._active is None:
            return
        try:
            self._active.tick()
        except Exception as exc:  # noqa: BLE001 — cv2/PIL can raise on decode
            self._drop_active(exc)
            return

        if len(self._items) <= 1:
            # 1-item case — never advance internally, but DO flip
            # `complete` after the item's natural duration so the
            # coordinator swaps us out for the rotation effect
            # instead of looping the same frame for `idle_seconds`.
            if len(self._items) == 1:
                elapsed = time.monotonic() - self._phase_start
                if elapsed >= self._items[0]["duration"]:
                    self.complete = True
            return

        elapsed = time.monotonic() - self._phase_start
        # Find the active item (whose renderer is self._active).
        active_item = next((it for it in self._items if it.get("renderer") is self._active), None)
        if active_item is None:
            # Active renderer was popped externally — pick a new one.
            self._cycle_advance()
            return
        if elapsed >= active_item["duration"]:
            # Honor hold_seconds — if the cumulative display window
            # already exceeds the configured hold, stop advancing.
            # The coordinator's hold-mode clock will fire on its own
            # when hold_seconds elapses from the out→in transition;
            # here we just ensure the cycler doesn't keep cycling
            # past hold_seconds for a multi-item list.
            if elapsed >= self._hold_seconds:
                # Mark current as fully shown but don't auto-advance.
                # The coordinator handles the hold → text_out
                # transition on its own clock.
                return
            self._cycle_advance()

    def _drop_active(self, exc: Exception) -> None:
        """Drop the currently-active item due to a tick-side decode failure."""
        active_item = next((it for it in self._items if it.get("renderer") is self._active), None)
        if active_item is None:
            self._active = None
            return
        logger.warning(
            "MediaCycler: dropping item %r due to decode failure: %s",
            active_item.get("url"),
            exc,
        )
        self._drop_item(active_item)
        if not self._items:
            self.exhausted = True
            self._active = None
            return
        # Pick a new item; recursion is bounded by len(self._items).
        self._cycle_advance()

    def render(self, canvas) -> None:
        """Forward `render` to the active inner renderer."""
        if self._active is None:
            return
        try:
            self._active.render(canvas)
        except Exception as exc:  # noqa: BLE001 — D12 codec failures
            self._drop_active(exc)
            if self._active is not None:
                # The next pick rendered something — try once so the
                # frame doesn't go fully black.
                try:
                    self._active.render(canvas)
                except Exception:  # noqa: BLE001
                    pass

    # -- introspection (test + observability) --------------------------------

    @property
    def active_url(self) -> str:
        """The S3 key of the currently-active item (or '' if none)."""
        active_item = next((it for it in self._items if it.get("renderer") is self._active), None)
        return active_item["url"] if active_item else ""

    @property
    def items_remaining(self) -> int:
        """Count of items still in the working list (after codec drops)."""
        return len(self._items)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_fetcher(url: str, *, api_key: str = "") -> bytes:
    """Fetch `url` and return the response body bytes.

    Uses `requests` with `allow_redirects=True` so the Flask 302 to
    the signed S3 URL is followed transparently — the cycler just
    needs the final bytes. Lazy-imported so host-side tests don't
    pull `requests` into the import graph at module load.

    `api_key` is sent as the `X-API-Key` header when non-empty. The
    Flask `/api/media/<key>` route is gated by `@api_login_required`,
    which checks `X-API-Key` before falling back to the browser
    session — the Pi has no session cookie, so without this header
    every fetch 401s and the cycler drops the item (D12).
    """
    import requests  # type: ignore[import-not-found]

    headers = {"X-API-Key": api_key} if api_key else {}
    resp = requests.get(url, timeout=10, headers=headers)
    resp.raise_for_status()
    return resp.content


def _build_default_fetcher(api_key: str) -> Callable[[str], bytes]:
    """Bind an api_key into the default fetcher.

    Returns a `(url: str) -> bytes` callable that injects the api_key
    as `X-API-Key` on every request. Used by `MediaCycler.__init__`
    to construct its default fetcher when the caller didn't supply
    one. The closure shape matches the `fetcher=` contract so
    downstream code (`self._fetcher(proxy_url)`) is unchanged.
    """

    def fetcher(url: str) -> bytes:
        return _default_fetcher(url, api_key=api_key)

    return fetcher


def _ext_for_mime(mime: str) -> str:
    """Return a sensible `.ext` for a MIME type (with leading dot).

    Used by `MediaCycler._resolve_local_path` when the S3 key has no
    extension (the `.bin` fallback from `_media_key`). Twilio MMS
    video on iPhone/Android is `video/3gpp` (H.263 in a 3GP container)
    — the browser preview's `<video>` element can't infer the codec
    from the `.bin` filename alone. Listing `.3gp` here is a hint, not
    a guarantee: OpenCV's `VideoCapture` sniffs by content and would
    open a `.bin` containing 3GP bytes just the same.
    """
    table = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "video/x-msvideo": ".avi",
        "video/x-matroska": ".mkv",
        "video/webm": ".webm",
        "video/3gpp": ".3gp",
        "video/3gpp2": ".3g2",
    }
    # Strip MIME parameters (e.g. `; charset=binary`, `; codecs="h263"`)
    # before lookup — Twilio sends parameterized Content-Types that
    # otherwise don't match the table. See `s3._safe_ext` for the
    # server-side sibling of this fix.
    return table.get(((mime or "").split(";", 1)[0].strip().lower()), "")
