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
over the recent pool runs only at the one background→out
transition (`idle_timeout`), so a 60Hz tick is cheap. Round 4
(queue redesign): the `fresh_id` trigger was removed; new
arrivals are picked off the FIFO at the natural pick sites.
`current_messages` is still read once per tick for the cycler
fall-back check — that's an in-memory deque slice, not a pull.

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
        # `_last_display_message` is the cached body for the next
        # fade-in, set by `_pick_next` at every `_begin_out` site and
        # consumed by out→in. Round 3 (debug-visibility): the pick
        # moved INTO `_begin_out` callers — every transition is now
        # driven by a freshly-picked message, so the fade-out log
        # carries the effect+trigger only (no stale `last_text=`).
        self._last_display_message: str | None = None
        # `_last_picked_entry` is set by `get_display_message()` to
        # the `MessageView` it picked, so callers (e.g. the out→in
        # transition) can read the picked message's `media` list and
        # decide whether to construct a `MediaCycler`. Reset to None
        # on every `get_display_message()` call.
        self._last_picked_entry: MessageView | None = None
        self._last_shown_message_id: str | None = None
        # Operator-debugging visibility (round 6, "selection algorithm
        # verbose logging"): the existing `_last_shown_message_id` is
        # the skip-sentinel for the re-roll loop in `_pick_next` /
        # `get_display_message`. It carries only the id, so when the
        # operator sees "the same message keeps getting selected", the
        # journal doesn't tell them which body / sender that id
        # corresponded to. `_last_selected_message_id` is the same id,
        # kept in sync at every pick site — just under a clearer
        # name. `_last_selected_body` / `_last_selected_sender` are
        # the human-readable companions, set alongside, for grep-
        # friendly logs (`rg "_last_selected_body=" journal`).
        self._last_selected_message_id: str | None = None
        self._last_selected_body: str | None = None
        self._last_selected_sender: str | None = None
        # Round 4 (queue redesign): the `_consumed_message_ids` set
        # is removed entirely. It was a "have we ever shown this id?"
        # sentinel used by the `hold` and `background` branches to
        # detect fresh-id arrivals mid-cycle and trigger an immediate
        # fade-out. With the FIFO queue (`MessageManager._new_messages_queue`),
        # fresh-id detection happens at the buffer-write site
        # (`_handle_message`) and the next natural pick drains the
        # queue — no mid-cycle interrupt, no consumed-id bookkeeping.
        # The media-cycler-exempt "_last_suppressed_message_id" gate
        # goes with it: no interrupt, no suppression log to gate.
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
            # The "no picked entry" condition is already conveyed by
            # the selection log's `(no picked entry — rotation)`
            # annotation emitted at the out→in site. The old INFO
            # line here was redundant — drop it.
            return None
        media = getattr(picked.message, "media", None) or []
        if not media:
            # Same rationale: the selection log's pretty-printed JSON
            # already shows `media=[]` and the body. The old INFO
            # line that duplicated that info is gone (ISC-22).
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

    def _pick_next(self) -> str | None:
        """Pick the next body to display, with an id-based skip.

        Round 3 (debug-visibility): every `_begin_out` site now calls
        this to decide which message the sign will show next. The
        caller is responsible for emitting the `Coordinator: selected`
        log via `_emit_selected_log` if the pick succeeds.

        Round 4 (queue): drain the FIFO of fresh arrivals FIRST,
        then fall back to the recent-pool random pick. New SMS
        arrivals always take precedence over a random re-roll — they
        were the user's reason for texting and deserve the next
        natural pick site. The queue is consumed one entry per
        call; rapid SMS accumulate in arrival order.

        The skip is keyed on `_last_shown_message_id` (an existing
        field maintained by `get_display_message`) — NOT on a body
        string. The previous body-based re-roll depended on
        `self.last_shown_text`, which left stale text in the fade-out
        log when media-only messages cycled through (the round-3
        bug). An id is a stable identifier; tracking it leaks no
        body text and survives media-only cycles.

        The skip is bounded to 4 re-rolls so a single-message pool
        (rare in practice — `recent_count` is typically 5+) cannot
        spin. If all 4 re-rolls land on the same id, we return
        whatever the last try produced — the bound is "always return
        something pickable, never None on a non-empty buffer."

        Side effects: writes `self._last_display_message` to the
        picked body, and `self._last_picked_entry` to the picked
        `MessageView`.

        Round 6 (verbose logging): every branch emits a `[select]`
        log line that names the picked id, body, sender, the queue
        depth before and after, the buffer size, the re-roll count,
        and the prior `_last_selected_message_id`. Operators can
        grep `journalctl -u lindsay_50 | grep '[select]'` to see
        the full pick decision tree without enabling DEBUG.
        """
        mm = self.message_manager
        # `getattr` with a 0 default keeps legacy test stubs (which
        # only shim `take_next_new_message`) from blowing up on
        # this new logging probe. The real `MessageManager` exposes
        # `new_message_queue_depth()` (added round 6) — stubs that
        # predate that addition only need the queue-drain entry
        # point to satisfy the contract.
        queue_depth_before = getattr(mm, "new_message_queue_depth", lambda: 0)()
        buffer_size = len(self.current_messages)
        log.info(
            "[select] _pick_next ENTRY queue_depth=%d buffer_size=%d last_selected_id=%s",
            queue_depth_before,
            buffer_size,
            self._last_selected_message_id,
        )

        # Round 4 (queue redesign): drain one entry off the FIFO
        # before the recent-pool random pick. The picked entry
        # already came from the live MQTT envelope, so its
        # `_last_picked_entry` / `_last_display_message` /
        # `_last_shown_message_id` are exactly the three fields the
        # out→in transition reads (cycler rebuild on `_last_picked_entry`,
        # scroller.set_text on `_last_display_message`, no-immediate-
        # re-pick on `_last_shown_message_id`). Writing them here
        # keeps the rest of the state machine unchanged.
        queue_msg = mm.take_next_new_message()
        if queue_msg is not None:
            self._last_picked_entry = queue_msg
            self._last_shown_message_id = queue_msg.message.id
            self._last_selected_message_id = queue_msg.message.id
            self._last_selected_body = queue_msg.message.body
            self._last_selected_sender = queue_msg.message.sender
            body = queue_msg.message.body
            self._last_display_message = body
            log.info(
                "[select] QUEUE_DRAIN source=queue msg_id=%s sender=%s body=%r "
                "queue_depth_before=%d queue_depth_after=%d",
                queue_msg.message.id,
                queue_msg.message.sender,
                (body or "")[:80],
                queue_depth_before,
                getattr(mm, "new_message_queue_depth", lambda: 0)(),
            )
            return body
        # Round 7 (re-roll loop bug fix): capture the "already
        # shown" id BEFORE the first `get_display_message()`
        # call, so the re-roll loop has a stable comparison
        # target. `get_display_message()` updates
        # `_last_shown_message_id` on EVERY call (HEAD_PRIORITY
        # and RANDOM both set it to the freshly-picked id), so
        # referencing the live field inside the loop makes the
        # check `picked.message.id == self._last_shown_message_id`
        # always TRUE after the first iteration — the loop
        # always ran all 4 iterations, oscillating between
        # HEAD_PRIORITY and RANDOM (operator-visible as the
        # round 7 live trace). The intended round-3 semantics
        # — "don't repeat a message that was on the sign
        # before this pick attempt" — now actually fires.
        # `None` skip_id is the boot state (no message has been
        # shown yet); the loop body's `skip_id is not None`
        # guard handles that — no re-roll possible anyway
        # because the first pick can't equal None.
        skip_id = self._last_shown_message_id
        # Fall through to the recent-pool random pick (unchanged).
        body = self.get_display_message()
        if body is None:
            log.info(
                "[select] BUFFER_EMPTY returning=None queue_depth=%d buffer_size=%d",
                getattr(mm, "new_message_queue_depth", lambda: 0)(),
                len(self.current_messages),
            )
            return None
        picked = self._last_picked_entry
        # Only re-roll if the buffer has more than one entry to choose
        # from. A 1-entry buffer makes re-rolling impossible (any pick
        # returns the same id); doing it anyway just inflates the
        # `get_display_message` call count for no semantic gain, and
        # trips the "≤ 2 pulls per transition" contract test.
        distinct_ids_in_buffer = len({e.message.id for e in self.current_messages})
        if distinct_ids_in_buffer <= 1:
            self._last_display_message = body
            log.info(
                "[select] SINGLE_ENTRY_BUFFER short-circuit picked_id=%s body=%r "
                "(no re-roll possible; buffer has %d distinct ids)",
                picked.message.id if picked else None,
                (body or "")[:80],
                distinct_ids_in_buffer,
            )
            return body
        tries = 0
        while (
            picked is not None
            and picked.message.id == skip_id
            and skip_id is not None
            and tries < 4
        ):
            body = self.get_display_message()
            picked = self._last_picked_entry
            tries += 1
        self._last_display_message = body
        log.info(
            "[select] RANDOM_PICK source=buffer msg_id=%s sender=%s body=%r "
            "distinct_ids=%d rerolls=%d skip_id_was=%s",
            picked.message.id if picked else None,
            picked.message.sender if picked else None,
            (body or "")[:80],
            distinct_ids_in_buffer,
            tries,
            skip_id,
        )
        # Keep the human-readable companions in sync with the id.
        if picked is not None:
            self._last_selected_message_id = picked.message.id
            self._last_selected_body = picked.message.body
            self._last_selected_sender = picked.message.sender
        return body

    def _resolve_next_effect_name(self) -> str:
        """Return the class name of the effect that will render the
        currently-picked entry at the next out→in. Read-only peek —
        does NOT mutate `self.idx` or `self.current`.

        The decision mirrors `_maybe_build_media_cycler` without its
        side effects:

          - If the picked message has a non-empty `media` list, the
            cycler will overlay the rotation effect for the hold:
              - Host (Pi): `MediaCycler`
              - Browser preview: `BrowserMediaOverlay`
          - Otherwise: the rotation effect at `(self.idx + 1) % len(self.effects)`.

        Used at the pick site so the `Coordinator: selected` log
        can carry the effect name on the SAME line as the picked
        message — round 4 (debug-visibility): operator wants the
        "what" (msg id + body) and "how" (effect) in a single log,
        before the fade-out line.
        """
        entry = self._last_picked_entry
        media = list(getattr(entry.message, "media", []) or []) if entry is not None else []
        if media:
            return "BrowserMediaOverlay" if self._is_browser else "MediaCycler"
        if not self.effects:
            return "?"
        next_idx = (self.idx + 1) % len(self.effects)
        return type(self.effects[next_idx]).__name__

    def _emit_selected_log(self, effect_name: str | None = None) -> None:
        """Emit the `Coordinator: selected` log for the picked body.

        Round 4 (debug-visibility): the selected log carries the
        picked message AND the effect that will render it, all in
        one record. The operator's first journal line for any
        transition now answers "what is on the sign and how is it
        being rendered" — `msg_id + body + effect` plus a pretty-
        printed JSON of the full `Message`. The follow-on `starting
        fade out` and `starting fade in` lines keep the timeline
        without repeating the same facts.

        The `effect_name` argument is optional — when the pick
        happens with an empty buffer (intro_done + nothing
        buffered, cycler_complete + nothing buffered) the caller
        has nothing to log and falls back to the `<no picked
        entry>` summary. When the caller provides a name, it's the
        resolved "next effect" via `_resolve_next_effect_name()`.
        """
        import json as _json

        entry = self._last_picked_entry
        if entry is None:
            log.info("Coordinator: selected <no picked entry>")
            return
        msg = entry.message
        media_list = list(getattr(msg, "media", []) or [])
        media_types = ", ".join(sorted({m.get("type", "?") for m in media_list}))
        type_suffix = f" ({media_types})" if media_types else ""
        # Round 4: effect name folds into the same summary line. A
        # missing name (empty pick path) shows `effect=?` so the
        # line shape is stable for log scrapers — operators grep
        # for `effect=MediaCycler`, `effect=Honeycomb`, etc., and
        # see exactly which effect will own the next hold.
        effect_part = f"effect={effect_name} " if effect_name else "effect=? "
        summary = (
            f"Coordinator: selected "
            f"msg_id={msg.id} "
            f"sender={msg.sender} "
            f"{effect_part}"
            f"media={len(media_list)}{type_suffix} "
            f"body={msg.body!r}"
        )
        json_body = _json.dumps(msg.to_dict(), indent=2, ensure_ascii=False)
        # Single log call with embedded newlines so the block
        # appears as one record in journalctl (matches the
        # round-2 selected-log shape operators are used to).
        log.info("%s\n  %s", summary, json_body.replace("\n", "\n  "))

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

        Round 3 (debug-visibility): the fade-out path now PICKS a
        fresh message instead of clearing `_last_picked_entry` to
        fall back to rotation. Previously the cycler-exhaust branch
        explicitly dropped the picked entry so the next out→in
        MediaCycler rebuild returned None (rotation effect, no
        scroller text) — the operator saw `(no picked entry —
        rotation)` in the selection log. Now: pick first (with the
        same id-based skip as every other transition site), then
        begin the fade-out. The cycler must transition regardless
        of whether the buffer has a message; if the pick is empty,
        we log `cycler_complete, no replacement` and proceed.
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
        # Round 3 (debug-visibility): the cycler-exhaust path now
        # PICKS a fresh message instead of clearing the picked entry
        # and falling back to a rotation effect with no message.
        # Previously `_last_picked_entry = None` here caused the
        # next `out` rebuild to take the rotation branch with no
        # scroller text — the operator saw `(no picked entry —
        # rotation)` in the selection log and no message body to
        # confirm what was on the sign. Now: pick first, emit the
        # selected-log, then begin the fade-out. The cycler is
        # exhausted; we must transition regardless of whether the
        # buffer has a message (rare, but possible during a flood
        # of media-only MMS that all consume simultaneously).
        picked_body = self._pick_next()
        if picked_body is not None:
            # Round 4: resolve and pass the effect name so the
            # selected-log carries msg + body + effect in one record.
            # `_last_picked_entry` was just set by `_pick_next`.
            self._emit_selected_log(self._resolve_next_effect_name())
        else:
            log.info(
                "Coordinator cycler_complete, no replacement — rotation will run for the rest of the hold"
            )
        # Trigger the existing fade-out machinery. `out` mode fades
        # `self.current` (the cycler) to 0, advances `self.idx`,
        # swaps to the next rotation effect at brightness 0, and
        # transitions to `in` for the fade-up. The `_step_fade`
        # throttle handles per-step palette writes during the ramp.
        self._begin_out_trigger = "cycler_complete"
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

        Round 6 (verbose logging): emits `[select-get]` lines that name
        the buffer size, head id, last-shown id, the chosen branch
        (HEAD_PRIORITY vs RANDOM), and the picked entry. Operators
        grep `journalctl -u lindsay_50 | grep '[select-get]'` to
        follow the recent-pool pick site decisions in real time.
        """
        self._last_picked_entry = None
        entries = self.current_messages
        if len(entries) == 0:
            log.info("[select-get] BUFFER_EMPTY returning=None")
            return None
        head = entries[0]
        log.info(
            "[select-get] ENTRY buffer_size=%d head_id=%s head_body=%r "
            "last_selected_id=%s",
            len(entries),
            head.message.id,
            (head.message.body or "")[:80],
            self._last_selected_message_id,
        )
        if head.message.id != self._last_shown_message_id:
            self._last_shown_message_id = head.message.id
            self._last_picked_entry = head
            self._last_selected_message_id = head.message.id
            self._last_selected_body = head.message.body
            self._last_selected_sender = head.message.sender
            log.info(
                "[select-get] HEAD_PRIORITY picked_id=%s sender=%s body=%r "
                "(head differs from last_selected_id=%s)",
                head.message.id,
                head.message.sender,
                (head.message.body or "")[:80],
                self._last_shown_message_id,
            )
            return head.message.body
        picked = random.choice(entries)
        self._last_shown_message_id = picked.message.id
        self._last_picked_entry = picked
        self._last_selected_message_id = picked.message.id
        self._last_selected_body = picked.message.body
        self._last_selected_sender = picked.message.sender
        log.info(
            "[select-get] RANDOM branch=picked_id=%s sender=%s body=%r "
            "from %d entries (head matched last_selected_id=%s)",
            picked.message.id,
            picked.message.sender,
            (picked.message.body or "")[:80],
            len(entries),
            self._last_shown_message_id,
        )
        return picked.message.body

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
        # Round 3 (debug-visibility): this log fires for every
        # transition — boot's intro→out, idle timeout, fresh-id
        # interrupt, cycler-exhaustion. Three fields only:
        # mode, effect (the active effect that's about to fade out),
        # trigger (which transition reason fired).
        #
        # Round 3 dropped the `last_text=` field that used to carry
        # `self.last_shown_text`. That field was set only when text
        # was truthy on the out→in transition — for media-only MMS
        # (body='', the "I sent a pic!" case) it kept the body of
        # the *previous* message, which leaked into the fade-out
        # log well after the message had cycled off the sign. The
        # body of the message that was on the sign is the job of
        # the previous cycle's `Coordinator: selected` log; the
        # fade-out log carries effect + trigger only.
        log.info(
            "Coordinator: starting fade out from mode=%s effect=%s trigger=%s",
            self.mode,
            self.current_effect_name,
            getattr(self, "_begin_out_trigger", "-"),
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
        # populated by `_pick_next` at every `_begin_out` site
        # (cycler_complete, intro_done, background idle) and
        # consumed by out→in to set the scroller. Round 4 (queue
        # redesign): the `fresh_id_interrupt` trigger was removed;
        # the queue drains at pick sites instead. Round 3
        # (debug-visibility): the pick moved INTO `_begin_out`
        # callers, so `_last_display_message` is
        # guaranteed non-None at the out→in branch for every
        # transition except the intro_done + empty buffer edge
        # case (handled by the `else` branch below — clear
        # scroller, no text).
        text = self._last_display_message

        if mode == "intro":
            if now - self.phase_start >= effects_settings.intro_seconds:
                # Round 3 (debug-visibility): pick a message BEFORE
                # `_begin_out` so the `Coordinator: selected` log
                # fires first. The heart should fade out regardless
                # of whether the buffer has a message — if `_pick_next`
                # returns None, we still transition; the out→in
                # branch's `if text:` check skips the scroller set
                # and the sign lands in background with no text.
                # The picked body, when present, becomes the first
                # thing on the sign after the heart fades out.
                if self._pick_next() is not None:  # type: ignore[attr-defined]
                    # Round 4: include the effect name so the
                    # selected-log carries msg + body + effect.
                    self._emit_selected_log(self._resolve_next_effect_name())
                self._begin_out_trigger = "intro_done"
                self._begin_out(now)

        elif mode == "out":
            # Cross-fade the current effect + any text to black, then swap in
            # the next effect and (if there's text) the next message.
            if self._step_fade(now, fading_out=True):
                # `_last_display_message` is set by `_pick_next` at the
                # `_begin_out` site that kicked this fade-out — every
                # transition site (cycler_complete, intro_done,
                # background idle) calls `_pick_next` before
                # `_begin_out`, so the body is already on hand. Round 4
                # (queue redesign): the `fresh_id_interrupt` site was
                # removed; the FIFO drains at the same natural pick
                # sites.
                # Round 3 (debug-visibility) dropped the seed-once
                # `_pick_next` fallback here because it duplicated
                # the pick that already happened upstream; the
                # fallback at the intro_done site (empty buffer) is
                # the only path where `_last_display_message` can be
                # None at this site — and that path takes the
                # `else` branch below (clear scroller, no text).
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
                    # Round 4 (queue redesign): the consumed-id
                    # bookkeeping (`_consumed_message_ids.add(...)`
                    # for both the picked body and the head of the
                    # recent pool) is removed. There is no longer a
                    # "fresh-id vs already-consumed" check anywhere
                    # in the state machine — fresh arrivals are
                    # picked off the FIFO at the natural pick sites
                    # (cycler_complete, intro_done, background idle)
                    # and the picked entry's id is what matters
                    # going forward (set on `_last_shown_message_id`
                    # by `_pick_next`).
                else:
                    scroller.set_text("", display.width)
                    self.showing_text = False
                # Round 3 (debug-visibility): the
                # `Coordinator: selected` log moved to the pick site
                # (fired by `_emit_selected_log` from each
                # `_begin_out` caller), so it appears BEFORE the
                # `starting fade out` line rather than buried here.
                # The single remaining log at out→in is the brief
                # `starting fade in` line — operator sees the
                # selected message → fade out → fade in.
                log.info(
                    "Coordinator: starting fade in effect=%s idx=%d",
                    self.current_effect_name,
                    self.idx,
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
                # Round 4 (debug-visibility): the `fade in done`
                # log is dropped entirely. The selected-log at the
                # pick site carried the picked msg + effect; the
                # `starting fade in` log at out→in marks the
                # transition. Operators don't need a third line
                # confirming the fade has completed — that's an
                # internal timing detail, not a sign-state event.
                self.mode = next_mode

        elif mode == "hold":
            # MediaCycler fall-back (issue #38): if the cycler is
            # exhausted (every attachment failed to decode or the
            # list is now empty), swap it back to the rotation
            # effect for the remainder of the hold. No-op when
            # `self.current` is a normal Effect.
            self._maybe_fall_back_to_rotation()
            # Hold semantics (round 4 / queue redesign):
            #   - Stay on the current message until `hold_seconds`
            #     elapses. New SMS arrivals are NOT interrupts —
            #     they accumulate in the MessageManager FIFO and
            #     get picked at the natural hold→text_out →
            #     out→in transition. The previous "fresh-id
            #     interrupts" + media-cycler exemption + set-of-
            #     consumed-ids machinery is removed entirely; the
            #     queue drains at pick sites instead.
            #   - Random re-picks from already-shown messages do
            #     NOT interrupt the hold either; they only kick a
            #     re-roll in the `background` mode below.
            if now - self.phase_start >= effects_settings.hold_seconds:
                # Round 4: the `hold→text_out` log is dropped
                # entirely. Timing detail (`held_for=…`,
                # `hold_seconds=…`) isn't a sign-state event — the
                # operator reads the held message from the
                # selected-log at the START of the cycle, and the
                # next selected-log fires when the next message
                # lands. No mid-cycle state event is needed.
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
                # Round 4: the `text_out→background` log is dropped.
                # Same rationale as `hold→text_out` — internal
                # fade-mechanics, not a sign-state event the
                # operator needs to read.
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
            # Background semantics (round 4 / queue redesign):
            #   - After `idle_seconds` of sitting in background, run
            #     ONE `_pick_next()` (which drains the FIFO first,
            #     then falls back to `random.choice` over the recent
            #     pool) to pick the next body and transition.
            #   - New SMS arrivals do NOT interrupt background mode
            #     — they accumulate in the FIFO and get picked on
            #     the next idle-driven `_pick_next` call (or the
            #     very next transition if `idle_seconds` is small).
            #     The previous "fresh-id kicks a fade" trigger is
            #     removed entirely; the queue drains at pick sites.
            #
            # The pull is the meaningful unit of work here. Earlier
            # versions throttled a `get_display_message()` call to
            # ~4 Hz and gated the trigger on `text != last_shown_text`,
            # but that combination was broken: random.choice over a
            # 2-message pool returns a different body than
            # `last_shown_text` ~50% of the time, so the trigger fired
            # on essentially every pull instead of after `idle_seconds`.
            # The fix is to keep the timer (`idle_seconds`) and only
            # call `_pick_next()` here, when we actually have a
            # reason to.
            idle_elapsed_seconds = now - self.phase_start
            idle_elapsed = idle_elapsed_seconds >= effects_settings.idle_seconds
            if idle_elapsed:
                # Probe the queue + buffer once at the pick site so
                # the IDLE_ELAPSED log can carry the live state.
                # Round 7 (live-bug triage): kept here (rather than
                # at the top of every tick as in round 6) so the
                # log only fires when the natural pick site fires —
                # one INFO line per idle cycle, not one per
                # coordinator frame.
                queue_depth = getattr(self.message_manager, "new_message_queue_depth", lambda: 0)()
                buffer_size = len(self.current_messages)
                # Round 3 (debug-visibility): pick the next message
                # BEFORE `_begin_out` so the `Coordinator: selected`
                # log fires first, then the fade-out log. The pick
                # also tells us whether the buffer has anything to
                # show — if `_pick_next` returns None, the buffer
                # is empty (nothing to show even after the idle
                # wait), and we skip the transition. The background
                # mode is already "effect only, no text" — there's
                # no point fading out for nothing.
                log.info(
                    "[select-bg] IDLE_ELAPSED — calling _pick_next "
                    "queue_depth=%d buffer_size=%d",
                    queue_depth,
                    buffer_size,
                )
                picked_body = self._pick_next()
                if picked_body is not None:
                    # Round 4: include the effect name so the
                    # selected-log carries msg + body + effect.
                    self._emit_selected_log(self._resolve_next_effect_name())
                    self._begin_out_trigger = "idle"
                    self._begin_out(now)  # show the queued message
                else:
                    log.info(
                        "[select-bg] PICK_RETURNED_NONE — staying in background "
                        "queue_depth=%d buffer_size=%d",
                        queue_depth,
                        buffer_size,
                    )
                    # Round 7 (live-bug triage, "BACKGROUND_TICK
                    # flood" / "IDLE_ELAPSED on every tick"): reset
                    # `phase_start` so the NEXT attempt to pick
                    # fires after another `idle_seconds` rather
                    # than every single tick. The intent of the
                    # background loop is "fire one pick attempt
                    # per idle_seconds"; resetting on the empty-
                    # path turns the loop into that cadence.
                    # Without it, an empty queue + empty buffer
                    # would log `[select-bg] IDLE_ELAPSED` and
                    # `[select-bg] PICK_RETURNED_NONE` at 60 Hz
                    # indefinitely until a message arrives.
                    self.phase_start = now
                # else: no message to show — stay in background.
                # The idle wait elapsed but there's no message to
                # fade to, so we silently keep rendering the current
                # effect until the next idle cycle.

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
