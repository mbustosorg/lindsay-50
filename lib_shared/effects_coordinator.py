"""Shared coordinator for the lifecycle state machine on the Pi and in the browser.

Both the Pi device (`heart-matrix-controller/main.py`) and the browser preview
(`heart-message-manager/preview_main.py`) drive the same six-mode fade state
machine for the sign's lifecycle:

    intro    — a beating heart for `intro_seconds`, no text.
    out      — cross-fade the current effect + any text to black.
    in       — fade the next effect (+ message text) up.
    hold     — keep a message fully visible for `hold_seconds`.
    text_out — fade only the text out, leaving the background effect lit.
    background — just the effect, no text, until the next message or idle.

The composite step (clear canvas, draw effect, draw scroller, swap to panel)
is polymorphism on the `display` (a `DisplayBase` subclass), not the
coordinator — `tick()` ends with one line: `self.display.render(...)`.

The render layer is OPTIONAL: an unbound coordinator (`bind(...)` not yet
called) is the shape `app_main.py` instantiates at PyScript startup. The
preview page's `preview_main.py` is the per-page shim that calls
`bind(display, scroller, effects, heart)` once the page-local canvas +
scroller + effects are constructed. `tick()` is a no-op when the
coordinator has no render layer (the app is alive on every admin page
but the canvas only exists on /preview).

The coordinator is the consumer; `MessageManager` is the source of truth.
`message_manager` is a required constructor argument. `tick()` does NOT
poll `manager.messages.get_messages(...)` on a timer — random.choice
over the recent pool runs only at the two background→out transitions
(`fresh_id` and `idle_timeout`), so a 60Hz tick is cheap. The
current_messages buffer-read for `fresh_id_landed` is the only
per-tick access; that's just an in-memory deque slice, not a pull.

Pacing comes from an `EffectsSettings` block (the v2 config shape),
passed as `settings` (an `EffectsSettings` instance or `None` for
the historic per-kwarg defaults).
"""

import logging
import random
import time

from lib_shared.display_base import DisplayBase
from lib_shared.effect_base import Effect
from lib_shared.message_manager import MessageManager
from lib_shared.models import MessageView, EffectsSettings, TextSettings
from lib_shared.scroller_base import ScrollerBase

log = logging.getLogger("heart")


def build_effects(
    effects_settings: EffectsSettings | None,
    effect_class_factory=None,
    *,
    display=None,
) -> list:
    """Build the effects rotation from an `EffectsSettings` block.

    Iterates `effects_settings.effects` in declared order. For each
    enabled entry, calls `effect_class_factory(name)` to resolve the
    Effect class, then instantiates it with `display` (every Effect
    constructor takes the display as its first positional arg).
    Disabled entries are skipped. Names the factory doesn't
    recognize are skipped silently (already logged inside the
    factory).

    If the resulting rotation is empty (every entry disabled or every
    name unknown), falls back to the first canonical effect from the
    declared rotation order so the sign never goes dark. The fallback
    is deterministic — same config always picks the same fallback —
    so the sign's idle behavior is predictable across reloads. If
    even the fallback is unresolvable (every canonical name is
    unknown to the factory), the rotation stays empty; callers that
    need a non-empty list at any cost should provide their own
    effects.

    Args:
        effects_settings: The v2 `EffectsSettings` block from `SignConfig`,
            or None (returns [] — there's no settings to derive a
            fallback from).
        effect_class_factory: Callable `name -> type | None`. Defaults
            to `lib_shared.effects_loader.make_effect_class`. Tests
            can pass a stub that returns simple effect classes.
        display: The display object handed to each Effect's constructor.
            Required when `effects_settings` is not None — every Effect
            subclass needs a display.

    Returns:
        A list of instantiated Effect objects in the order
        declared in `effects_settings.effects`. Enabled effects
        only. Empty when `effects_settings is None` or when the
        fallback is also unresolvable.
    """
    if effects_settings is None:
        return []
    if effect_class_factory is None:
        from lib_shared.effects_loader import make_effect_class

        effect_class_factory = make_effect_class
    if display is None:
        raise ValueError("build_effects requires `display` to instantiate effects")
    out = []
    for entry in effects_settings.effects:
        name = entry.get("name", "")
        enabled = entry.get("enabled")
        if not enabled:
            continue
        cls = effect_class_factory(name)
        if cls is None:
            continue
        out.append(cls(display))
    if not out:
        # Fallback: the first canonical effect (the head of the
        # declared rotation order). Resolved through the same
        # factory so an unknown canonical name is caught and
        # skipped — the rotation stays empty in that case (better
        # dark panel than a crash). Deterministic: same config
        # always picks the same fallback, so the sign's idle
        # behavior is predictable across reloads.
        for entry in effects_settings.effects:
            cls = effect_class_factory(entry.get("name", ""))
            if cls is not None:
                log.warning(
                    "build_effects: rotation empty after filter, " "falling back to first canonical effect %r",
                    entry.get("name"),
                )
                return [cls(display)]
    return out


class EffectsCoordinator:
    """Drives the boot splash, message lifecycle, and idle rotation.

    Used directly by both the Pi entrypoint and the browser preview;
    no subclass. The coordinator is constructed unbound (no
    `display`/`scroller`/`effects`/`heart`); the page that owns the
    canvas (the preview's `preview_main.py`) calls `bind(...)` once
    those objects are in scope. `tick()` no-ops when the coordinator
    is unbound so an admin page that loaded the app-scoped
    coordinator can run a no-rAF idle without crashing.

    The coordinator is the consumer; `MessageManager` is the source
    of truth. `message_manager` is a required keyword argument —
    there is no fallback to a None manager. The constructor raises
    `TypeError` if a caller omits it. `tick()` calls
    `get_display_message()` throttled to ~4 Hz; the cached result
    becomes the next text shown by the fade state machine.

    Args:
        message_manager: required `MessageManager` instance. The
            coordinator reads messages and config from it. Construct
            one before constructing the coordinator and pass it in.
            The coordinator holds no copy of the config; pacing,
            rotation, and text settings are observed live via
            `message_manager.get_effects_settings()` /
            `.get_text_settings()` at tick time. Small structural
            diffs (`_last_rotation`, `_last_text_color`,
            `_last_text_speed`) gate the rotation rebuild and
            scroller setter calls when nothing has changed.
        fade_step: throttle (seconds) between palette writes during a fade.
        gamma: gamma exponent applied to the linear fade progress.
    """

    def __init__(
        self,
        message_manager: MessageManager,
        display: DisplayBase | None = None,
        scroller: ScrollerBase | None = None,
        effects: list[Effect] | None = None,
        heart: Effect | None = None,
        fade_step: float = 0.04,
        gamma: float = 2.2,
        *,
        media_api_base_url: str = "",
        media_cache_dir: str = "",
        media_api_key: str = "",
        is_browser: bool = False,
    ) -> None:
        # Required — no default. Raises TypeError if a caller omits it.
        # All live config (pacing fields, rotation, text settings) is
        # read from `message_manager.config` at tick time — the
        # coordinator holds no copy, so config updates land
        # automatically without an explicit `apply_settings` call.
        self.message_manager = message_manager
        self.display = display
        self.scroller = scroller
        self.effects = list(effects) if effects is not None else []
        self.heart = heart
        # Throttles palette writes during a fade. Without this, a fast main loop
        # rewrites the palette far faster than the panel refreshes, wasting work.
        self.fade_step = fade_step
        # Gamma correction: linear time → perceptually linear brightness.
        self.gamma = gamma
        # MediaCycler wiring (issue #38). The coordinator constructs
        # one when the picked message has a non-empty `media` list;
        # the cycler replaces `self.effects[self.idx]` for the hold.
        # `media_api_base_url` is the Flask server origin (e.g.
        # "http://localhost:3100") — the cycler builds
        # "{api_base_url}/api/media/{s3_key}" URLs. `media_cache_dir`
        # is the local directory for downloaded bytes (None / "" means
        # the OS temp dir).
        self._media_api_base_url = media_api_base_url or ""
        self._media_cache_dir = media_cache_dir or ""
        # X-API-Key for the Pi's MediaCycler fetcher. Sent as a request
        # header so Flask's `@api_login_required` on `/api/media/<key>`
        # recognizes the request as a machine client. Same value as
        # `cfg.API_SECRET_KEY` on the Flask server — the Pi and Flask
        # share the secret via their respective settings.toml.
        self._media_api_key = media_api_key or ""
        # `is_browser` toggles between two media render paths:
        # host/Pi builds a `MediaCycler` that decodes each
        # attachment with PIL/cv2 and blits it onto the rgbmatrix
        # canvas (real-display fidelity); preview/browser builds a
        # `BrowserMediaOverlay` that hands the same Flask proxy URL
        # to the DOM `<img>` / `<video>` elements that `preview.js`
        # drives — the browser handles decoding natively so we
        # don't need OpenCV in Pyodide. Mirrors the existing
        # `MessageManager(is_browser=True)` flag.
        self._is_browser = bool(is_browser)

        self.idx = -1  # first message shown advances this to 0
        self.current = heart  # effect being rendered right now (may be None)
        self.mode = "intro"
        self.fade_start = 0.0
        self.last_step = 0.0
        self.phase_start = 0.0  # start of intro / hold / background
        self.showing_text = False
        self.last_shown_text = None
        # `_last_display_message` is the cached body for the next
        # fade-in, set by `_pick_next_text` at the background→out
        # transition and consumed by out→in. There is no per-tick
        # pull — `get_display_message()` (which does random.choice)
        # is only called at the two transition paths below.
        self._last_display_message: str | None = None
        # `_last_picked_entry` is set by `get_display_message()` to
        # the `MessageView` it picked, so callers (e.g. the out→in
        # transition) can read the picked message's `media` list and
        # decide whether to construct a `MediaCycler`. Reset to None
        # on every `get_display_message()` call.
        self._last_picked_entry: MessageView | None = None
        self._last_shown_message_id: str | None = None
        # `_consumed_message_ids` is the set of message ids that have
        # actually FADED IN on the scroller during this process's
        # lifetime. Distinct from `_last_shown_message_id`, which
        # `get_display_message` mutates on every pull (random.choice
        # included) — too noisy to use as a "is this a genuinely new
        # SMS?" sentinel because random.choice over a multi-message
        # pool bounces the comparison and falsely trips every other
        # pull. We need the full history (a set, not a single id)
        # because a "fresh" id is one that has never been shown yet;
        # recency of the *last* consumed id is not enough when there
        # are N ≥ 2 messages in the recent pool.
        self._consumed_message_ids: set[str] = set()
        # Last message id whose fresh-id interrupt was suppressed by
        # the media-cycler exemption. Gates the "suppressing fresh-id
        # interrupt" INFO log so it fires once per fresh-id (on the
        # transition from "no suppression" → "suppressing"), not on
        # every tick of the same suppressed id. (Without this, every
        # background-mode tick fired the log because the top entry
        # stays unconsumed until the cycler completes — ~5ms cadence,
        # so the log spammed ~200 identical lines/sec.)
        self._last_suppressed_message_id: str | None = None
        # Render-layer diff state: structural fingerprints of the
        # rotation list and text settings, used to gate the
        # rotation rebuild and scroller setter calls in `tick()`.
        # Not a copy of the source-of-truth config — the values
        # live on `message_manager.config` and are read live each
        # tick; these fields just let us skip the rebuild /
        # setter when nothing has changed. Initialized to the
        # signature of the default `EffectsSettings()` /
        # `TextSettings()` so the first tick after construction
        # is a no-op when the manager's config is still at its
        # default (the typical boot state — the v2 config
        # arrives over MQTT shortly after and the diff then
        # fires, triggering the rebuild). The bootstrap
        # `effects=` / scroller color / speed constructor args
        # survive until that first real config update.
        # `bind()` resets all three to None so the first tick
        # after a fresh bind refreshes the now-attached render
        # layer.
        self._last_rotation: tuple | None = tuple((e.get("name"), e.get("enabled")) for e in EffectsSettings().effects)
        self._last_text_color: int | None = TextSettings().color
        self._last_text_speed: int | None = TextSettings().speed

    def is_bound(self) -> bool:
        """True when the coordinator has a render layer (display + scroller + effects + heart).

        An unbound coordinator is a no-op: `tick()` returns without
        touching state. The Pi always binds before its first tick
        (it constructs the display + scroller + effects in the
        same module-level pass). The browser's `app_main.py`
        instantiates the coordinator unbound; the preview page
        binds once the canvas is in scope.
        """
        d = self.display is not None
        s = self.scroller is not None
        e = bool(self.effects)
        h = self.heart is not None
        return d and s and e and h

    def bind(
        self,
        display: DisplayBase | None = None,
        scroller: ScrollerBase | None = None,
        effects: list[Effect] | None = None,
        heart: Effect | None = None,
    ) -> None:
        """Attach (or swap) the render layer.

        Replaces `display`/`scroller`/`effects`/`heart` in place.
        `heart` defaults to the head of the new effects list when
        not supplied (preview uses the first effect as the
        boot-splash; the Pi passes an explicit Heartbeat instance).

        The rotation / scroller text-settings caches are reset on
        bind, so the first `tick()` after bind refreshes the
        render layer with the manager's current config (the
        app-scoped coordinator's `on_change` callback fires
        before the preview has had a chance to `bind`, so the
        pre-bind ticks were no-ops that never touched the
        render layer).

        Safe to call mid-life: the next `tick()` uses the new
        layer. The Pi's `main.py` calls it once at startup; the
        browser's `preview_main.py` calls it once the page-local
        canvas is constructed.
        """
        self.display = display
        self.scroller = scroller
        self.effects = list(effects) if effects is not None else []
        self.heart = heart if heart is not None else (self.effects[0] if self.effects else None)
        if self.current is None:
            self.current = self.heart
            if self.heart is not None:
                self.heart.set_brightness(1.0)
            self.phase_start = time.monotonic()
        # Reset the render-layer diff sentinels so the next tick
        # rebuilds the rotation and reapplies the scroller color /
        # speed against the now-attached render layer. The
        # manager's config is the source of truth; these fields
        # are just structural diffs against it.
        self._last_rotation = None
        self._last_text_color = None
        self._last_text_speed = None

    def start(self) -> None:
        """Begin the boot splash.

        No-op when the coordinator is unbound — the app-scoped
        coordinator on non-preview admin pages is constructed
        without a render layer; `start()` is only meaningful on
        the preview's per-page shim. The first message after the
        heart fades out comes from the manager's buffer via
        `get_display_message()`, which the throttled tick pulls
        from — there is no separate "show this body after the
        heart" hook (the push path is gone).
        """
        if not self.is_bound():
            return
        assert self.heart is not None
        self.current = self.heart
        self.heart.set_brightness(1.0)
        self.mode = "intro"
        self.phase_start = time.monotonic()

    @property
    def current_messages(self) -> list[MessageView]:
        """Returns the current consideration set of messages to display,
        ie. the latest unsupressed recent_count messages"""
        return self.message_manager.get_messages(limit=self.effects_settings.recent_count, suppress=True)

    @property
    def effects_settings(self) -> EffectsSettings:
        """Returns the current effects settings"""
        return self.message_manager.get_effects_settings()

    @property
    def text_settings(self) -> TextSettings:
        """Returns the current text settings"""
        return self.message_manager.get_text_settings()

    def _consumed_message_id_at_pick(self, body: str) -> str | None:
        """Return the id of the buffered message whose body matches `body`.

        Called at the out→in transition to mark "this is the message we
        just faded in." The buffer (held by the manager) is searched in
        recent-first order so the most-recent match wins — relevant when
        the same body was sent twice (rare but possible). Returns None
        when the buffer no longer holds the body (e.g. evicted by a
        flood of newer messages before the fade-in completed); the
        caller treats that as "no consumption tracking" and the next
        fresh-id check defaults to a no-match.
        """
        for entry in self.current_messages:
            if entry.message.body == body:
                return entry.message.id
        return None

    def _maybe_build_media_cycler(self) -> Effect | None:
        """Construct a `MediaCycler` (Pi) or `BrowserMediaOverlay`
        (preview) for the picked message's MMS media.

        Called at the out→in transition. Returns:
          - A media effect if the picked message has a non-empty
            `media` list AND we have a display to construct it
            against. The effect takes over `self.current` for the
            hold.
          - None otherwise (SMS-only messages, no display, or no
            picked entry — e.g. the intro→out path that didn't
            actually pick a message).

        Which effect gets constructed depends on `is_browser`:
          - Host (Pi): `MediaCycler` decodes with PIL/cv2 and blits
            through the `Bitmap`/`Palette` pipeline (real-display
            fidelity — every LED pixel is driven by Python).
          - Preview: `BrowserMediaOverlay` carries the same cycle
            logic but exposes the active attachment to the JS-side
            DOM `<img>` / `<video>` elements via three read-only
            properties. The browser handles decoding natively — no
            OpenCV in Pyodide, no PyScript bytes round-trip.

        Both paths handle codec failures (D12): if the working list
        becomes empty, `exhausted` is True at construction and the
        coordinator's `hold` branch falls back to the rotation
        effect via `_maybe_fall_back_to_rotation`.
        """
        picked = self._last_picked_entry
        if picked is None:
            log.info(
                "Coordinator media-cycler: no picked entry; rotation effect will run instead",
            )
            return None
        media = getattr(picked.message, "media", None) or []
        if not media:
            log.info(
                "Coordinator media-cycler: picked message has empty media; "
                "rotation effect will run (message_id=%s body=%r)",
                picked.message.id,
                picked.message.body,
            )
            return None
        if self.display is None:
            log.info(
                "Coordinator media-cycler: no display bound (browser preview, no canvas); "
                "skipping media override message_id=%s",
                picked.message.id,
            )
            return None
        hold_seconds = self.message_manager.get_effects_settings().hold_seconds
        if self._is_browser:
            # Lazy import — same rationale as the host branch.
            from lib_shared.patterns.browser_media_overlay import BrowserMediaOverlay

            return BrowserMediaOverlay(
                picked.message.id,
                media,
                api_base_url=self._media_api_base_url,
                hold_seconds=hold_seconds,
            )
        # Lazy import — MediaCycler pulls in Pillow (ImageDisplay) and
        # optionally cv2 (VideoDisplay). The host test suite has Pillow
        # but not cv2; keeping the import here avoids loading it at
        # coordinator-import time.
        from lib_shared.patterns.media_cycler import MediaCycler

        return MediaCycler(
            picked.message.id,
            media,
            display=self.display,
            api_base_url=self._media_api_base_url,
            hold_seconds=hold_seconds,
            cache_dir=self._media_cache_dir or None,
            api_key=self._media_api_key,
        )

    def _maybe_fall_back_to_rotation(self) -> None:
        """If `self.current` is a `MediaCycler` or `BrowserMediaOverlay`
        that's done (exhausted or complete), trigger the existing
        fade-out machinery so the cycler fades to black and the
        rotation effect fades in for the next cycle.

        Called at every `hold`, `text_out`, and `background` tick.
        Idempotent: when `self.current` is not one of the media
        effects (the typical case), this is a no-op. When it IS and
        still has items, the cycler keeps running — the
        coordinator's existing `hold_seconds` clock decides when to
        transition out.

        The cycler classes extend `Effect` and add `exhausted: bool`
        and `complete: bool`. On the host path we test
        `isinstance(current, MediaCycler)`; on the browser path the
        cycler helper returned a `BrowserMediaOverlay` instead
        (PIL/cv2 aren't in the PyScript bundle). Both classes share
        the same `exhausted` / `complete` contracts, so the same
        fallback logic applies — duck-type on the flags rather than
        `isinstance`, so the browser preview ALSO gets a fallback
        when an attachment's URL 404s / the DOM overlay had every
        item rejected. Without this, a browser preview with no
        playable media sits on the boot `<img>` / `<video>` and
        never returns to a rotation effect — the canvas below the
        overlay is black for the rest of the hold.

        The `MediaCycler` import is guarded: the browser preview's
        PyScript bundle does NOT include `lib_shared.patterns.media_cycler`
        (the cycler pulls in PIL + cv2 + a filesystem cache — none of
        which Pyodide can satisfy). `try/except ImportError` around
        the import turns "module missing in bundle" into "only the
        browser side of the isinstance check", which is the right
        behavior — the duck-typed flag branch still works.

        The fade-out path delegates to `_begin_out(now)` so the
        crossfade is driven by the same `_step_fade` machinery every
        other mode transition uses — no parallel fade code, no
        duplicate throttling. We clear `_last_picked_entry` first so
        the `out` mode's MediaCycler rebuild at fade-complete returns
        None (we want the rotation effect, not a fresh cycler for
        the same message).
        """
        try:
            from lib_shared.patterns.media_cycler import MediaCycler as _MediaCycler
        except ImportError:
            # Pi-style cycler isn't loadable here (browser preview
            # bundle). The BrowserMediaOverlay path below still
            # handles the browser side of the fallback.
            _MediaCycler = None  # type: ignore[assignment]

        # Same lazy-import dance for the browser-side overlay. The
        # CPython host test suite doesn't have the browser_media_overlay
        # module on its path the way PyScript does — module isn't
        # bundled in the host package. When that's the case, fall
        # back to `object` so the isinstance check is False and the
        # branch is just skipped (mirrors MediaCycler's behavior).
        try:
            from lib_shared.patterns.browser_media_overlay import BrowserMediaOverlay as _BrowserOverlay
        except ImportError:
            _BrowserOverlay = object  # type: ignore[assignment,misc]

        current = self.current
        if current is None:
            return
        is_media_cycler = _MediaCycler is not None and isinstance(current, _MediaCycler)
        is_browser_overlay = isinstance(current, _BrowserOverlay)
        if not (is_media_cycler or is_browser_overlay):
            return
        # Both cyclers extend `Effect` and add `exhausted` and
        # `complete` — pyright can't see them through `isinstance`
        # narrowing, so the attribute access is annotated.
        # `exhausted` = codec failure (D12, every item dropped);
        # `complete` = cycler played everything it was given (1-item
        # ran for `item["duration"]` seconds; multi-item cycled
        # through every attachment once). Both trigger the same
        # fade-out-to-rotation behavior — the cycler is done either
        # way, and the rotation effect should take over for the
        # remainder of the hold/idle window.
        is_done = bool(current.exhausted) or bool(getattr(current, "complete", False))  # type: ignore[attr-defined]
        if not is_done:
            return
        # If we're already mid-fade-out (the previous tick triggered
        # this), let the existing `out` mode complete naturally —
        # re-entering here would just restart the fade clock.
        if self.mode == "out":
            return
        # The cycler was just swapped in by an out→in transition
        # and brightness is climbing back to 1.0. Firing another
        # fade-out mid fade-in would oscillate. Bail; the cycler's
        # `complete` / `exhausted` flags stay set, so the next tick
        # in `hold` / `background` will pick up the fade.
        if self.mode == "in":
            return
        effects = self.effects
        if not effects:
            return
        reason = "exhausted" if getattr(current, "exhausted", False) else "complete"  # type: ignore[attr-defined]
        log.info(
            "Coordinator media-cycler %s (%s): fading out for rotation effect=%s",
            reason,
            "BrowserMediaOverlay" if is_browser_overlay else "MediaCycler",
            self.current_effect_name,
        )
        # Clear the picked entry so the `out` mode's cycler rebuild
        # at fade-complete returns None (we want the rotation
        # effect, not a fresh cycler for the same message — the
        # cycler just finished playing everything it had).
        self._last_picked_entry = None
        # Trigger the existing fade-out machinery. `out` mode fades
        # `self.current` (the cycler) to 0, advances `self.idx`,
        # swaps to the next rotation effect at brightness 0, and
        # transitions to `in` for the fade-up. The `_step_fade`
        # throttle handles per-step palette writes during the ramp.
        self._begin_out(time.monotonic())

    def _current_is_active_media_cycler(self) -> bool:
        """True when `self.current` is a media cycler (host or
        browser) that hasn't yet completed its natural playback.

        Used by the `hold` and `background` branches to suppress
        fresh-id interrupts while a media cycler is still
        delivering its content (10s for images, video length for
        videos). Without this guard, a text-only SMS arriving 2
        seconds after an image interrupts the image mid-display;
        with the guard, the cycler plays out its full window and
        the new SMS gets picked at the next background→out
        transition.

        Duck-typed via the `exhausted` / `complete` flags added
        by `MediaCycler` and `BrowserMediaOverlay` — both
        cyclers set `exhausted=True` for codec failures and
        `complete=True` after their natural duration elapses.
        A cycler with either flag set is "done" and shouldn't
        block the interrupt (the coordinator's existing
        `_maybe_fall_back_to_rotation` will handle the
        transition).
        """
        current = self.current
        if current is None:
            return False
        # Lazy-import the cycler classes for the same reason
        # `_maybe_fall_back_to_rotation` does — the browser
        # preview's PyScript bundle doesn't ship `media_cycler.py`,
        # and we don't want to bind it here either.
        try:
            from lib_shared.patterns.media_cycler import MediaCycler as _MediaCycler
        except ImportError:
            _MediaCycler = None  # type: ignore[assignment]
        try:
            from lib_shared.patterns.browser_media_overlay import BrowserMediaOverlay as _BrowserOverlay
        except ImportError:
            _BrowserOverlay = object  # type: ignore[assignment,misc]
        is_media_cycler = _MediaCycler is not None and isinstance(current, _MediaCycler)
        is_browser_overlay = isinstance(current, _BrowserOverlay)
        if not (is_media_cycler or is_browser_overlay):
            return False
        # Active = cycler is in place AND neither flag has fired.
        # `getattr(..., "complete", False)` covers the legacy path
        # where a cycler class might not have the attribute — those
        # cyclers are always "still playing" from the coordinator's
        # perspective (the existing hold_seconds clock handles the
        # cutoff in that case).
        if getattr(current, "exhausted", False):  # type: ignore[attr-defined]
            return False
        if getattr(current, "complete", False):  # type: ignore[attr-defined]
            return False
        return True

    def get_display_message(self) -> str | None:
        """Pick the body to display next, from the manager's buffered messages.

        Algorithm:
          1. Read `message_manager.config.effects_settings.recent_count`
             most-recent non-suppressed messages.
          2. If the list is empty, return None.
          3. If the head entry's id differs from `self._last_shown_message_id`,
             return its body and update `_last_shown_message_id` (fresh-message
             priority).
          4. Otherwise pick uniformly at random from the list and return that
             entry's body, updating `_last_shown_message_id` to the picked id.

        Side effect: also stores the picked entry on `self._last_picked_entry`
        so callers (e.g. the out→in transition) can read the picked
        message's `media` list and decide whether to construct a
        `MediaCycler`. The side channel is reset to None at the start
        of every call so callers always see the most recent pick.

        Returns:
            The body string to show next, or None when the buffer is empty.
        """
        self._last_picked_entry = None
        entries = self.current_messages
        if len(entries) == 0:
            return None
        head = entries[0]
        if head.message.id != self._last_shown_message_id:
            self._last_shown_message_id = head.message.id
            self._last_picked_entry = head
            return head.message.body
        picked = random.choice(entries)
        self._last_shown_message_id = picked.message.id
        self._last_picked_entry = picked
        return picked.message.body

    def _pick_next_text(self) -> str | None:
        """Pick the next body to display, re-rolling if random.choice
        lands on the body we just showed.

        Called at the two background→out transition paths (`new_id`
        and `idle`). This is the ONLY call to `get_display_message`
        in the coordinator's hot path — `random.choice` does not run
        on a timer. Re-rolls are bounded (5 tries) so a single-message
        pool can't spin: if all 5 tries land on `last_shown_text`,
        we return whatever the last try gave us.

        `get_display_message()` itself does the random.choice and
        the fresh-id-vs-random branching; we just wrap it with the
        "avoid the body we just showed" loop on top.
        """
        body = self.get_display_message()
        if body is None:
            return None
        tries = 0
        while body == self.last_shown_text and tries < 4:
            body = self.get_display_message()
            tries += 1
        return body

    def _step_fade(self, now, fading_out, fade_effect=True, fade_text=True):
        """Advance the active fade one throttled step; return True when complete."""
        progress = (now - self.fade_start) / self.message_manager.config.effects_settings.fade_seconds
        if progress > 1.0:
            progress = 1.0
        if now - self.last_step >= self.fade_step or progress >= 1.0:
            self.last_step = now
            linear = (1.0 - progress) if fading_out else progress
            b = linear**self.gamma
            current = self.current
            scroller = self.scroller
            if fade_effect and current is not None:
                current.set_brightness(b)
            if fade_text and scroller is not None:
                scroller.set_brightness(b)
        return progress >= 1.0

    def _begin_out(self, now):
        # `_begin_out` fires for two distinct reasons:
        #   1. intro-second elapses (first-ever fade after boot)
        #   2. a fresh SMS arrives during hold/background and interrupts
        # The Pi can't toggle LOG_LEVEL at runtime, so log both reasons at
        # INFO level — operators need to see sign-lifecycle events in the
        # journal without service-restart gymnastics. The `trigger` kwarg
        # disambiguates the two paths.
        scroller_text = ""
        if self.scroller is not None:
            scroller_text = self.scroller.text or ""
        log.info(
            "Coordinator._begin_out: from mode=%s effect=%s scroller_text=%r",
            self.mode,
            self.current_effect_name,
            scroller_text,
        )
        self.mode = "out"
        self.fade_start = now
        self.last_step = 0.0

    @property
    def current_effect_name(self):
        """The class name of the active effect (browser status block)."""
        if self.current is None:
            return ""
        return type(self.current).__name__

    @property
    def current_text(self):
        """The body of the message currently being scrolled (or '' for idle)."""
        if self.scroller is None:
            return ""
        return self.scroller.text or ""

    def tick(self):
        """Advance the state machine one frame.

        No-op when the coordinator is unbound (an admin page
        without a canvas). When bound, the lifecycle runs
        intro → out → in → hold → text_out → background, the
        current effect's `tick()` advances, the scroller
        scrolls, and `display.render(...)` composites the
        frame.

        Pulls the next display message from the manager on a
        ~4 Hz throttle (`_PULL_INTERVAL`). Non-pull ticks consume
        the cached result from the previous pull.

        Pacing fields (intro_seconds, hold_seconds, etc.) and the
        rotation / scroller text settings are read live from
        `message_manager.config`; no per-coordinator copy, no
        explicit `apply_settings` call needed. Config updates land
        at most one frame later.
        """
        try:
            self._tick_inner()
        except BaseException:
            raise

    def _tick_inner(self):
        if not self.is_bound():
            return
        # Local aliases for the bound layer — Pyright doesn't narrow
        # `self.display` etc. through `is_bound()`, but the guard above
        # makes these accesses safe.
        display = self.display
        scroller = self.scroller
        effects = self.effects
        assert display is not None
        assert scroller is not None
        assert effects

        # Refresh the render layer from the manager's current config.
        # The manager is the single source of truth — read the
        # settings live via the `effects_settings` / `text_settings`
        # properties (which delegate to `message_manager.get_*`).
        # The coordinator holds no copy of the config; the small
        # structural diffs (`_last_rotation`, `_last_text_color`,
        # `_last_text_speed`) just gate the rotation rebuild and
        # scroller setter calls when nothing has changed. Pacing
        # fields are read at the call sites below directly from
        # the manager.
        effects_settings = self.effects_settings
        rotation = tuple((e.get("name"), e.get("enabled")) for e in effects_settings.effects)
        if rotation != self._last_rotation:
            log.info(
                "Coordinator rotation rebuild: prev=%s new=%s",
                self._last_rotation,
                rotation,
            )
            self.effects = build_effects(effects_settings, display=display)
            self.idx = -1  # next fade picks the head of the new list
            self._last_rotation = rotation
        text_settings = self.text_settings
        if text_settings.color != self._last_text_color:
            log.info(
                "Coordinator scroller color change: prev=%s new=%s",
                self._last_text_color,
                text_settings.color,
            )
            scroller.set_color(text_settings.color)
            self._last_text_color = text_settings.color
        if text_settings.speed != self._last_text_speed:
            log.info(
                "Coordinator scroller speed change: prev=%s new=%s",
                self._last_text_speed,
                text_settings.speed,
            )
            scroller.set_speed(text_settings.speed)
            self._last_text_speed = text_settings.speed

        now = time.monotonic()
        mode = self.mode

        # `text` is the cached body for the next fade-in. It is
        # populated by `_pick_next_text` at the background→out
        # transition (the ONLY place we run random.choice — see
        # `tick()` docstring) and consumed by out→in to set the
        # scroller. Before the first transition, `text` is None
        # and the sign shows its background effect with no text.
        text = self._last_display_message

        if mode == "intro":
            if now - self.phase_start >= effects_settings.intro_seconds:
                self._begin_out(now)

        elif mode == "out":
            # Cross-fade the current effect + any text to black, then swap in
            # the next effect and (if there's text) the next message.
            if self._step_fade(now, fading_out=True):
                # `_last_display_message` is set by the background→out
                # transition (the only path that pulls during normal
                # operation). The intro→out path (the first-ever fade
                # after boot, or any path that bypassed background)
                # doesn't have a pulled value yet — pull once here so
                # the sign has text to show on the fade-in. Without
                # this, text would be None, the sign would enter
                # background immediately, and the very next tick would
                # fire `fresh_id_landed` and loop back through out→in
                # with idx already advanced — never landing in hold.
                if self._last_display_message is None:
                    seeded = self._pick_next_text()
                    if seeded is not None:
                        self._last_display_message = seeded
                text = self._last_display_message
                self.idx = (self.idx + 1) % len(effects)
                self.current = effects[self.idx]
                self.current.set_brightness(0.0)
                # MMS media override (issue #38): if the picked
                # message has a non-empty `media` list, swap a
                # `MediaCycler` in place of the rotation effect. The
                # cycler takes over `self.current` for the duration of
                # the hold, cycling through the attachments
                # (D4/D5/D12). On `exhausted` the coordinator falls
                # back to `self.effects[self.idx]` (the rotation entry
                # we just selected) for the remainder of the hold.
                media_override = self._maybe_build_media_cycler()
                if media_override is not None:
                    self.current = media_override
                    self.current.set_brightness(0.0)
                if text:
                    scroller.set_text(text, display.width)
                    scroller.set_brightness(0.0)
                    self.showing_text = True
                    self.last_shown_text = text
                    # Mark BOTH the picked body's id AND the head's id
                    # as "consumed" — a follow-on hold or background
                    # should not treat either as fresh. A genuine
                    # fresh-id interrupt requires an id that has never
                    # been consumed (i.e. an SMS we haven't seen yet).
                    #
                    # The head's id matters more than the picked body's
                    # here, because `get_display_message` does
                    # `random.choice(entries)` and the head (newest) is
                    # almost never the pick when `recent_count` is much
                    # larger than 1. Without this second add, the
                    # `fresh_id_landed` check in the `hold` and
                    # `background` branches would compare the head
                    # against an empty set and trip every cycle,
                    # cycling the state machine at the fade rate
                    # instead of honoring `hold_seconds` /
                    # `idle_seconds`. Symptom: "each background only
                    # shows for ~4s, just the fade-in + fade-out."
                    consumed_at_pick = self._consumed_message_id_at_pick(text)
                    if consumed_at_pick is not None:
                        self._consumed_message_ids.add(consumed_at_pick)
                    if self.current_messages:
                        head_id = self.current_messages[0].message.id
                        self._consumed_message_ids.add(head_id)
                else:
                    scroller.set_text("", display.width)
                    self.showing_text = False
                log.info(
                    "Coordinator out→in: idx=%d effect=%s text=%r showing_text=%s media_override=%s",
                    self.idx,
                    self.current_effect_name,
                    text if text else "",
                    self.showing_text,
                    "yes" if media_override is not None else "no",
                )
                self.mode = "in"
                self.fade_start = now
                self.last_step = 0.0

        elif mode == "in":
            if self._step_fade(now, fading_out=False):
                assert self.current is not None
                self.current.set_brightness(1.0)
                scroller.set_brightness(1.0)
                self.phase_start = now
                next_mode = "hold" if self.showing_text else "background"
                log.info(
                    "Coordinator in→%s: effect=%s text=%r",
                    next_mode,
                    self.current_effect_name,
                    self.last_shown_text or "",
                )
                self.mode = next_mode

        elif mode == "hold":
            # MediaCycler fall-back (issue #38): if the cycler is
            # exhausted (every attachment failed to decode or the
            # list is now empty), swap it back to the rotation
            # effect for the remainder of the hold. No-op when
            # `self.current` is a normal Effect.
            self._maybe_fall_back_to_rotation()
            # Hold semantics (v2):
            #   - Stay on the current message until `hold_seconds` elapses,
            #     UNLESS a genuinely *new* SMS arrives — i.e. the head of the
            #     recent pool has an id we haven't shown yet. Random re-picks
            #     from already-shown messages do NOT interrupt the hold;
            #     they only kick a re-roll in the `background` mode below.
            #   - The previous comparison (`text != self.last_shown_text`)
            #     compared BODY strings, which meant `random.choice` over a
            #     5-entry pool could land on a different body every pull and
            #     effectively keep hold duration clamped to the throttle
            #     interval (~0.25s). That bug is why `hold_seconds` was
            #     observed "taking a long time, then disappearing after a
            #     few seconds" — every random pick interrupted the hold
            #     instantly. The fix: gate the interrupt on the ID, not the
            #     body, and idle `hold_seconds` otherwise.
            #   - **Media cycler exemption** (issue #38 follow-up): when
            #     the active effect is a MediaCycler / BrowserMediaOverlay
            #     that hasn't completed its natural duration (10s for
            #     images, video length for videos), suppress fresh-id
            #     interrupts. Without this, a 1-second-old image fades
            #     out the moment a fresh text-only SMS arrives — the
            #     operator sees the image for ~2 seconds (fade-in + a
            #     sliver of `hold`) instead of the 10-second window
            #     the cycler is supposed to deliver. The new SMS queues
            #     up in the buffer and gets picked at the next
            #     background→out transition once the cycler completes.
            fresh_id_landed = (
                self.current_messages and self.current_messages[0].message.id not in self._consumed_message_ids
            )
            if fresh_id_landed and self._current_is_active_media_cycler():
                fresh_id_landed = False
                suppressed_id = self.current_messages[0].message.id
                # Log only on transition (newly suppressed id) — same
                # id stays suppressed across every tick of the
                # cycler's natural duration.
                if suppressed_id != self._last_suppressed_message_id:
                    self._last_suppressed_message_id = suppressed_id
                    log.info(
                        "Coordinator hold: suppressing fresh-id interrupt while media cycler active effect=%s",
                        self.current_effect_name,
                    )
            if fresh_id_landed:
                log.info(
                    "Coordinator hold interrupt (new id): pending_text=%r last_shown=%r new_id=%s",
                    text,
                    self.last_shown_text,
                    self.current_messages[0].message.id,
                )
                self._begin_out(now)  # new SMS interrupts the hold
            elif now - self.phase_start >= effects_settings.hold_seconds:
                log.info(
                    "Coordinator hold→text_out: effect=%s held_text=%r held_for=%.1fs hold_seconds=%.1f",
                    self.current_effect_name,
                    self.last_shown_text,
                    now - self.phase_start,
                    effects_settings.hold_seconds,
                )
                self.mode = "text_out"
                self.fade_start = now
                self.last_step = 0.0

        elif mode == "text_out":
            # MediaCycler fall-back (issue #38 follow-up): if the
            # cycler completes during text_out, kick the fade-out
            # now so the rotation effect takes over at the same
            # time the text finishes clearing. Without this call,
            # we'd transition to `background` with a stale cycler
            # and only catch the fade on the next background tick —
            # the user sees one extra frame of the cycler with no
            # text, which looks like a hang.
            self._maybe_fall_back_to_rotation()
            # Only the text fades; the background effect stays lit.
            if self._step_fade(now, fading_out=True, fade_effect=False):
                scroller.set_text("", display.width)
                scroller.set_brightness(1.0)
                self.showing_text = False
                self.phase_start = now
                log.info(
                    "Coordinator text_out→background: effect=%s",
                    self.current_effect_name,
                )
                self.mode = "background"

        elif mode == "background":
            # MediaCycler fall-back (issue #38 follow-up): if the
            # cycler is exhausted or has completed playback of
            # every item, swap it back to the rotation effect for
            # the remainder of the idle window. Without this, an
            # empty-body MMS lands in background with
            # `self.current = MediaCycler` and the cycler loops the
            # same frame(s) for the full `idle_seconds` (60s by
            # default, was 5min before that). The user-visible
            # symptom: a 1-second screenshot-video sits on the
            # panel for the whole idle window before re-rolling.
            # With this call, the cycler signals `complete` after
            # `item["duration"]` (10s for images, video length for
            # videos) and we fall back here on the next tick.
            self._maybe_fall_back_to_rotation()
            # Background semantics (v2, pull-once):
            #   - A genuinely new SMS (head.id differs from last-shown) kicks
            #     a fade immediately — the operator just texted, show it now.
            #   - After `idle_seconds` of sitting in background with no
            #     fresh SMS, run ONE `get_display_message()` (which does
            #     `random.choice` over the recent pool) to pick the next
            #     body and transition.
            #
            # The pull is the meaningful unit of work here. Earlier
            # versions throttled a `get_display_message()` call to
            # ~4 Hz and gated the trigger on `text != last_shown_text`,
            # but that combination is broken: random.choice over a
            # 2-message pool returns a different body than
            # `last_shown_text` ~50% of the time, so the trigger fired
            # on essentially every pull instead of after `idle_seconds`.
            # The fix is to drop the timer entirely and only call
            # `get_display_message()` here, when we actually have a
            # reason to.
            entries_bg = self.current_messages
            fresh_id_landed = bool(entries_bg and entries_bg[0].message.id not in self._consumed_message_ids)
            # **Media cycler exemption** (issue #38 follow-up): when
            # the active effect is a MediaCycler / BrowserMediaOverlay
            # that hasn't completed its natural duration, suppress
            # fresh-id interrupts. Mirrors the same exemption in
            # `hold` mode. Without this, the cycler gets cut off
            # the moment a new SMS arrives, instead of playing
            # out its full 10s window. The new SMS queues up in
            # the buffer and gets picked at the next background→out
            # transition once the cycler completes.
            if fresh_id_landed and self._current_is_active_media_cycler():
                fresh_id_landed = False
                suppressed_id = entries_bg[0].message.id
                # Log only on transition (newly suppressed id) — same
                # id stays suppressed across every tick of the
                # cycler's natural duration. Without this gate the
                # INFO log fired every tick (~5ms cadence) and
                # spammed ~200 identical lines per second.
                if suppressed_id != self._last_suppressed_message_id:
                    self._last_suppressed_message_id = suppressed_id
                    log.info(
                        "Coordinator background: suppressing fresh-id interrupt while media cycler active effect=%s",
                        self.current_effect_name,
                    )
            idle_elapsed = now - self.phase_start >= effects_settings.idle_seconds

            trigger: str | None = None
            if fresh_id_landed:
                trigger = "new_id"
            elif idle_elapsed:
                trigger = "idle"

            if trigger is not None:
                # One pull per transition. `_pick_next_text` re-rolls
                # internally if `random.choice` happens to land on the
                # body we just showed (bounded to 5 tries — a
                # single-message pool can't spin).
                new_text = self._pick_next_text()
                if new_text is not None:
                    self._last_display_message = new_text
                log.info(
                    "Coordinator background→out (%s): waited=%.1fs idle_seconds=%.1f next_text=%r",
                    trigger,
                    now - self.phase_start,
                    effects_settings.idle_seconds,
                    new_text or "",
                )
                self._begin_out(now)  # show the queued message

        current = self.current
        assert current is not None
        # Defensive try/except: an exception inside the render
        # block (e.g. a freshly-staged Effect with a missing
        # attribute) would otherwise dump a traceback to the
        # journal once and then crash the loop on the next frame.
        # Re-raise so systemd's Restart=always still catches it
        # and the loader's exception path can record context.
        try:
            current.tick()
            scroller.tick(display.width)
            display.render(current, scroller)
        except BaseException:
            raise
