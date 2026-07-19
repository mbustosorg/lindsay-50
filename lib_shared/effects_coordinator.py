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
from types import SimpleNamespace

from lib_shared.display_base import DisplayBase
from lib_shared.effect_base import Effect
from lib_shared.message_manager import MessageManager
from lib_shared.models import Message, EffectsSettings, TextSettings
from lib_shared.scroller_base import ScrollerBase

# Pluggable message-selection (issue #26). The coordinator is
# selector-agnostic — it accepts any `MessageSelector` subclass via its
# `selector` kwarg (test override) and resolves the live default from
# `effects_settings.selector_algorithm` via `make_selector(...)` on
# every pick. Both `WeightedSelector` and `RandomSelector` are
# available; the operator picks between them on the admin /settings
# page (default: "weighted"). The eligibility filter that bounds the
# candidate pool runs through the shared `build_eligible_messages`
# helper — every selection algorithm sees the same pre-filtered set.
from lib_shared.selector import (
    MessageSelector,
    build_eligible_messages,
    make_selector,
)

log = logging.getLogger("heart")

# Behavioral knob: the post-hold idle gap. Per the project's
# behavioral-knobs-in-code rule (scoring weights, decay windows, and
# rollout flags live as module-level constants alongside the
# algorithm — NOT in settings.toml), this is a code constant.
# 3 seconds is the post-hold gap before the next message fades in —
# short enough that the operator sees quick rotation after a held
# message fades out, long enough to let the background effect breathe.
IDLE_SECONDS_AFTER_HOLD: float = 3.0


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
        event_log=None,
        selector: MessageSelector | None = None,
        favorites: list | tuple | set | None = None,
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
        # Pluggable selector wiring (issue #26). The coordinator is
        # selector-agnostic — it accepts any `MessageSelector` subclass
        # via its `selector` kwarg (test override; explicit instances
        # bypass `make_selector` entirely) and resolves the live
        # default from `effects_settings.selector_algorithm` via
        # `make_selector(...)` on every pick. Both `WeightedSelector`
        # and `RandomSelector` are available; the operator picks
        # between them on the admin /settings page (default:
        # "weighted"). The eligibility filter that bounds the
        # candidate pool runs through the shared
        # `build_eligible_messages` helper — every selection algorithm
        # sees the same pre-filtered set, regardless of which
        # `selector_algorithm` is active. When `event_log` is
        # supplied, the coordinator writes a `text_display` event to
        # the log at every out→in (no special-casing for pre-emption
        # — the on-deck model treats all fade-ins uniformly).
        # `favorites` is the optional message-id list consumed by the
        # selector's favorite boost — kept as a coordinator field (not
        # a global) so tests and the preview can change it without
        # reconstructing the coordinator.
        self._event_log = event_log
        self._selector_override = selector  # `None` → resolve via make_selector each pick
        self._favorites = favorites if favorites is not None else ()

        self.idx = -1  # first message shown advances this to 0
        self.current = heart  # effect being rendered right now (may be None)
        self.mode = "intro"
        self.fade_start = 0.0
        self.last_step = 0.0
        self.phase_start = 0.0  # start of intro / hold / background
        # Compatibility shims for the round-4 selected-log shape:
        # `_last_picked_entry` is the MessageView-shaped namespace
        # the selected-log reads (`entry.message.id`, etc.) — we
        # write it at every `_pick_message_via_selector` site.
        # `_last_display_message` is the body string `_step_fade`
        # reads during the fade-out log. Both default to None so the
        # first-tick code paths don't trip on missing attributes.
        self._last_picked_entry = None
        self._last_display_message = None
        # Two semantic slots replace the legacy `_last_*` shortcut
        # fields. `current_message` is the message currently being
        # rendered (consumed from `on_deck` at out→in, persists through
        # in/hold/text_out/background/out until the next out→in).
        # `on_deck` is the message picked for the next cycle — the
        # WeightedSelector picks it at out→in, a fresh SMS replaces
        # it, and it's consumed at the next out→in. The event log
        # records `current_message` as displayed at every out→in;
        # "have we ever shown this id?" is derived from the event
        # log (see `displayed_message_ids()` accessor).
        self.current_message: Message | None = None
        self.on_deck: Message | None = None
        # One-shot flag set by `_maybe_fall_back_to_rotation` when
        # a MediaCycler / BrowserMediaOverlay exhausts mid-hold and
        # the rotation effect should take over for the remainder of
        # the cycle. The next `_maybe_build_media_cycler` call (at
        # the upcoming out→in) sees the flag, returns None, and
        # resets it. This prevents a fresh cycler from being built
        # for the just-finished cycler's message — without dropping
        # `current_message` (we still want its body for the
        # scroller's post-fade-out text window). One-shot is
        # sufficient: the cycler is exhausted, so re-arming the
        # flag doesn't recur until a NEW cycler is staged and
        # later exhausts.
        #
        # `_suppress_for_message_id` carries the cycler's
        # `message_id` so the next out→in can distinguish "still
        # staging the same message whose cycler just exhausted"
        # (suppress) from "a fresh message with its own media"
        # (build a new cycler). Without the id sidecar the flag
        # leaks across message boundaries and a NEW MMS picked
        # during the next cycle would silently fall through to
        # the rotation effect instead of getting its own
        # cycler — observed in the browser preview's console
        # as `effect=Hyperspace` immediately after a
        # `BrowserMediaOverlay` for the previous message had
        # completed.
        self._suppress_media_override: bool = False
        self._suppress_for_message_id: str | None = None

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
    def effects_settings(self) -> EffectsSettings:
        """Returns the current effects settings"""
        return self.message_manager.get_effects_settings()

    @property
    def text_settings(self) -> TextSettings:
        """Returns the current text settings"""
        return self.message_manager.get_text_settings()

    @property
    def showing_text(self) -> bool:
        """True when the sign is currently displaying text from `current_message`.

        Derived from `current_message` rather than cached as a flag —
        the underlying state is the source of truth.
        """
        return self.current_message is not None and bool(self.current_message.body)

    def current_message_id(self) -> str | None:
        """The id of the message currently being rendered, or None.

        Reads from the `current_message` slot. Returns None during the
        intro phase (no message has been staged yet).
        """
        return self.current_message.id if self.current_message is not None else None

    def on_deck_message(self) -> Message | None:
        """The message picked for the next cycle, or None.

        The WeightedSelector populates this at `out→in`; a fresh SMS
        arrival replaces it (during hold / text_out / background / in
        / out). Consumed (becomes `current_message`) at the next
        `out→in`.
        """
        return self.on_deck

    def displayed_message_ids(self) -> set[str]:
        """All message ids that have ever been faded in on this device.

        Derived from the event log — every `text_display` event
        records a message_id, and the set of those ids is the
        "ever-shown" set. Used by the `has_unshown_message` and
        fresh-id pre-emption checks. Empty when no event log is
        bound (the coordinator still works without one — see the
        `_event_log is None` fallbacks in the fresh-id paths).
        """
        if self._event_log is None:
            return set()
        try:
            return {e["message_id"] for e in self._event_log.query(event_type="text_display")}
        except (AttributeError, TypeError, KeyError):
            return set()

    def has_unshown_message(self) -> bool:
        """True when the buffer head has an id that has never been faded in.

        A "fresh" message is one the event log doesn't have a
        `text_display` entry for. Used by the `hold` and
        `background` branches to decide whether a new SMS has
        arrived (and replace `on_deck` accordingly).
        """
        entries = self.message_manager.get_messages(limit=1, suppress=True)
        if not entries:
            return False
        shown = self.displayed_message_ids()
        return entries[0].message.id not in shown

    def _fresh_id_in_buffer(self) -> Message | None:
        """The fresh message at the buffer head, if any.

        A message is "fresh" iff it's neither the currently-rendered
        message nor (when an event log is bound) one we've ever shown
        before. Returns the head's `Message`, or None when no fresh
        message is queued.

        Used by the `hold`, `text_out`, and `background` branches
        of `tick()` to detect a new SMS and replace `on_deck`. With
        the new on-deck model, fresh-id handling is a silent slot
        swap — no `_begin_out` interrupt, no per-tick state changes.
        Hold runs to natural end; the fresh SMS shows up after the
        held message completes its lifecycle.

        Reads just the buffer head (`limit=1`) — fresh-id detection
        is a "is there a NEW SMS at the head?" check; the rest of
        the buffer isn't relevant to this branch.

        When no event log is bound we fall back to a head-vs-current
        check — the head is fresh if it differs from the currently-
        rendered message. This handles tests that drive the
        coordinator without an event log (the typical harness).
        """
        entries = self.message_manager.get_messages(limit=1, suppress=True)
        if not entries:
            return None
        head = entries[0].message
        # Head is the current message — no fresh arrival.
        if self.current_message is not None and head.id == self.current_message.id:
            return None
        if self._event_log is not None:
            shown = self.displayed_message_ids()
            if head.id in shown:
                return None
        return head

    def _consumed_message_id_at_pick(self, body: str) -> str | None:
        """Return the id of the eligible message whose body matches `body`.

        Called at the out→in transition to mark "this is the message we
        just faded in." The eligible set (built via
        `build_eligible_messages` with the live `lookback_days`) is
        searched in recent-first order so the most-recent match wins —
        relevant when the same body was sent twice (rare but possible).
        Returns None when the eligible set no longer holds the body
        (e.g. evicted from the ring buffer or aged out beyond the
        lookback window before the fade-in completed); the caller
        treats that as "no consumption tracking" and the next fresh-id
        check defaults to a no-match.

        Searching the eligible set (not the full buffer) keeps the
        consumed-id logic consistent with the selector pool — a
        message that just faded in MUST be in the eligible set (the
        body was just picked from it), so the lookup is the same scan
        the selector ran a moment ago.
        """
        candidates = build_eligible_messages(
            self.message_manager,
            now=time.time(),
            lookback_seconds=self.effects_settings.lookback_seconds,
        )
        for m in candidates:
            if m.body == body:
                return m.id
        return None

    def _refresh_render_layer_from_settings(self, display: "DisplayBase", scroller: "ScrollerBase") -> None:
        """Apply live settings from the manager to the render layer.

        Called only at cycle boundaries (the out→in transition). Tick
        itself is a pure render — settings are not re-applied every
        tick; this is the user's architectural split: "tick should
        just update the panel with what's currently rendering.
        Settings should refresh on the next cycle."

        Reads the manager's `effects_settings` and `text_settings`
        live. Three concrete applications:

        1. Rotation rebuild — rebuild the Effect instances from the
           current `effects_settings.effects` so a config edit
           between cycles (effect added/removed/disabled) shows up
           on the next out→in. `self.idx` is intentionally NOT
           reset here — the next out→in's `idx = (idx + 1) % len`
           advance picks from the new (possibly shorter) list, so
           removed effects drop out naturally and the rotation
           continues from wherever it was. Resetting idx to -1
           here would force the next cycle back to `effects[0]`
           every time, no matter what changed.
        2. Scroller color — applies `text_settings.color`.
        3. Scroller speed — applies `text_settings.speed`.

        Each branch logs the change at INFO level so operators can
        trace live config edits in the journal.
        """
        effects_settings = self.effects_settings
        log.info(
            "Coordinator rotation refresh at cycle boundary: %s",
            [e.get("name") for e in effects_settings.effects],
        )
        self.effects = build_effects(effects_settings, display=display)

        text_settings = self.text_settings
        log.info(
            "Coordinator scroller settings refresh: color=%s speed=%s",
            text_settings.color,
            text_settings.speed,
        )
        scroller.set_color(text_settings.color)
        scroller.set_speed(text_settings.speed)

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
        # `current_message` is the message being staged for fade-in
        # at the out→in transition; this is the source of truth for
        # the message whose media we're going to play. `Message` is
        # the flat record (id/body/media); the wrapping `MessageView`
        # is the manager's per-message envelope (source/rules/
        # suppressed).
        # One-shot guard for the cycler-fall-back path:
        # `_maybe_fall_back_to_rotation` arms this flag when a
        # cycler exhausts mid-hold. The next rebuild returns None,
        # the rotation effect takes over for the post-fade-out
        # window, and the flag resets. Without this guard the cycler
        # would simply rebuild for the same message and immediately
        # exhaust again on its second playback.
        #
        # The flag is scoped to the cycler's own message_id via
        # `_suppress_for_message_id` — see the id compare below.
        # If the picked message at this out→in transition has a
        # different id, the flag is for a different (already-
        # finished) cycler and a NEW cycler is the right answer.
        picked = self.current_message
        if picked is None:
            log.info(
                "Coordinator media-cycler: no current message; rotation effect will run instead",
            )
            return None
        if self._suppress_media_override:
            suppressed_for = self._suppress_for_message_id
            picked_id = getattr(picked, "id", None)
            if suppressed_for is None or suppressed_for == picked_id:
                # Same message whose cycler just exhausted — the
                # rebuild guard fires. Clear the flag and skip.
                self._suppress_media_override = False
                self._suppress_for_message_id = None
                log.info(
                    "Coordinator media-cycler: suppressed by cycler fall-back (message_id=%s); rotation effect will run instead",
                    picked_id,
                )
                return None
            # Different message — the cycler that exhausted was for
            # an earlier message whose fade-out is now complete.
            # Clear the stale flag and fall through to build a new
            # cycler for THIS message's media. Without this branch,
            # a fresh MMS picked during the next cycle would
            # silently fall through to the rotation effect instead
            # of getting its own cycler.
            log.info(
                "Coordinator media-cycler: stale suppression flag cleared (suppressed_for=%s picked=%s); building new cycler",
                suppressed_for,
                picked_id,
            )
            self._suppress_media_override = False
            self._suppress_for_message_id = None
        media = getattr(picked, "media", None) or []
        if not media:
            log.info(
                "Coordinator media-cycler: current message has empty media; "
                "rotation effect will run (message_id=%s body=%r)",
                picked.id,
                picked.body,
            )
            return None
        if self.display is None:
            log.info(
                "Coordinator media-cycler: no display bound (browser preview, no canvas); "
                "skipping media override message_id=%s",
                picked.id,
            )
            return None
        hold_seconds = self.message_manager.get_effects_settings().hold_seconds
        if self._is_browser:
            # Lazy import — same rationale as the host branch.
            from lib_shared.patterns.browser_media_overlay import BrowserMediaOverlay

            return BrowserMediaOverlay(
                picked.id,
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
            picked.id,
            media,
            display=self.display,
            api_base_url=self._media_api_base_url,
            hold_seconds=hold_seconds,
            cache_dir=self._media_cache_dir or None,
            api_key=self._media_api_key,
        )

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
        media: list = list(getattr(entry.message, "media", []) or []) if entry is not None else []
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

        The fade-out path delegates to `_begin_out(now)` so the
        crossfade is driven by the same `_step_fade` machinery every
        other mode transition uses — no parallel fade code, no
        duplicate throttling. We arm `_suppress_media_override` so
        the `out` mode's MediaCycler rebuild at fade-complete returns
        None (we want the rotation effect, not a fresh cycler for
        the same message). `current_message` stays set so the
        scroller keeps showing the just-finished cycler's body
        during the post-fade-out rotation window.
        """
        try:
            from lib_shared.patterns.media_cycler import MediaCycler as _MediaCycler
        except ImportError:
            # Pi-style cycler isn't loadable here (browser preview
            # bundle). The BrowserMediaOverlay path below still
            # handles the browser side of the fallback.
            _MediaCycler = None  # type: ignore[assignment,misc]

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
        # Trigger the existing fade-out machinery. `out` mode fades
        # `self.current` (the cycler) to 0, advances `self.idx`,
        # swaps to the next rotation effect at brightness 0, and
        # transitions to `in` for the fade-up. The `_step_fade`
        # throttle handles per-step palette writes during the ramp.
        # We arm `_suppress_media_override` so the next
        # `_maybe_build_media_cycler` call (at the upcoming out→in)
        # returns None — we want the rotation effect to take over
        # for the remainder of the hold/idle window, not a fresh
        # cycler for the just-finished cycler's message. Keeping
        # `current_message` set (NOT nulling it) is intentional: the
        # out→in staging reads its `.body` for the scroller's text
        # during the post-fade-out window.
        #
        # `_suppress_for_message_id` carries the cycler's
        # `message_id` so the suppress guard only fires for the
        # SAME message whose cycler just finished. A NEW message
        # picked at the next out→in has a different id and gets
        # a fresh cycler (the stale flag is cleared in
        # `_maybe_build_media_cycler`).
        cycler_message_id = getattr(current, "message_id", None)
        self._suppress_media_override = True
        self._suppress_for_message_id = cycler_message_id
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
            _MediaCycler = None  # type: ignore[assignment,misc]
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
        """Return the body of the message currently being rendered, or
        the on-deck body when nothing has been staged yet.

        Reads from the slot fields directly — no selector call, no
        `random.choice`, no fresh-id branch. The pick happens at the
        `out→in` transition (the only call site for
        `self._pick_message_via_selector(...)` in the hot path); `get_display_message`
        is just a slot reader so callers (preview JS, status
        endpoints, ad-hoc diagnostics) can ask "what's showing now?"
        without mutating coordinator state.

        During `intro` (no message staged yet), falls through to
        `on_deck` — the first selector pick populates `on_deck` at
        `intro→out`, so this returns whatever the upcoming out→in
        will fade in.

        Returns:
            The body string to show, or None when neither slot is set
            (e.g. an empty buffer with no picks yet).
        """
        if self.current_message is not None:
            return self.current_message.body
        if self.on_deck is not None:
            return self.on_deck.body
        return None

    def _pick_message_via_selector(
        self,
        exclude_id: str | None = None,
    ) -> Message | None:
        """Pick the next message from the eligible set via the configured selector.

        Called from `intro→out` (first pick, populates `on_deck`) and
        from `out→in` (every subsequent pick, overwrites `on_deck` with
        the message for the next cycle). The eligible set is built
        once per pick by `build_eligible_messages(...)` from the
        live `lookback_days` setting; the same pre-filtered list is
        handed to whatever selector `make_selector(selector_algorithm)`
        returns — both `WeightedSelector` and `RandomSelector` operate
        on identical inputs (the shared-pool design). Returns None
        when the eligible set is empty or the selector yields nothing
        — callers treat None as "no message for the next cycle;
        rotation-only display."

        `exclude_id` is the anti-repeat hint from the out→in call
        site: pass the just-consumed message's id so the next pick
        avoids back-to-back selection. Both selectors honor it —
        `RandomSelector` filters the candidate pool before
        `random.choice`; `WeightedSelector` filters before scoring.
        When the exclusion would empty the pool both selectors fall
        back to the unfiltered set so the sign keeps rotating.

        Algorithm dispatch: each pick reads
        `effects_settings.selector_algorithm` (live, so an admin UI
        change lands on the next pick without restart) and resolves
        it via `make_selector(...)`. The explicit `selector=` kwarg
        set at construction wins over the live setting — tests that
        pin a specific selector still get a deterministic pick.

        Note: the `current_event_type` is fixed to `"text_display"`
        here — the coordinator's text-display event is the
        display-recency signal. Future per-effect selectors (e.g. one
        for image cycler playback) would add a `current_event_type`
        kwarg; not needed today.
        """
        settings = self.effects_settings
        candidates = build_eligible_messages(
            self.message_manager,
            now=time.time(),
            lookback_seconds=settings.lookback_seconds,
        )
        if not candidates:
            return None
        selector = (
            self._selector_override
            if self._selector_override is not None
            else make_selector(settings.selector_algorithm)
        )
        picked = selector.pick(
            candidates,
            now=time.time(),
            event_log=self._event_log,
            favorites=self._favorites,
            event_type="text_display",
            exclude_id=exclude_id,
        )
        if picked is not None:
            return picked
        # Selector returned None (e.g. nothing eligible under the
        # weighted path). Fall back to a uniform random pick so the
        # sign still rotates instead of going dark. Same exclusion
        # logic — the candidate pool would otherwise include the
        # just-consumed message every time.
        if exclude_id is not None and candidates:
            filtered = [m for m in candidates if m.id != exclude_id]
            fallback = filtered or candidates
        else:
            fallback = candidates
        return random.choice(fallback) if fallback else None

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

        # Settings refresh happens at the cycle boundary (out→in),
        # NOT every tick. Tick is a pure render: read pacing fields
        # from the manager (live) and advance the state machine,
        # but don't rebuild effects or call scroller.set_color /
        # set_speed unless we're at the cycle boundary. This is the
        # architectural split the user pinned: tick = update the
        # panel with what's currently rendering; settings apply on
        # the next cycle.

        # Pacing fields are read at the call sites below directly
        # from the manager (via `self.effects_settings` / `self.text_settings`).
        effects_settings = self.effects_settings

        now = time.monotonic()
        mode = self.mode

        # The pick happens at `out→in`, not at `background→out`.
        # At `intro→out` we still seed `on_deck` so the very
        # first `out→in` consumes a message rather than running
        # with `on_deck=None` and producing a rotation-only
        # transition. All other pre-staging transitions leave
        # `on_deck` alone — a fresh-id arrival just replaces it.
        if mode == "intro":
            if now - self.phase_start >= effects_settings.intro_seconds:
                if self.on_deck is None:
                    self.on_deck = self._pick_message_via_selector()
                    # Round 4 (debug-visibility): selected-log fires
                    # at the pick site BEFORE `_begin_out`, so the
                    # operator reads `selected → fade out` rather than
                    # `fade out → selected`. The picked `on_deck`
                    # drives the next cycle's `out→in` consume.
                    if self.on_deck is not None:
                        self._last_picked_entry = SimpleNamespace(
                            message=self.on_deck,
                            source="selector",
                            suppressed=False,
                        )
                    self._emit_selected_log(self._resolve_next_effect_name())
                self._begin_out(now)

        elif mode == "out":
            # Cross-fade the current effect + any text to black, then
            # swap in the next effect and (if there's a message) the
            # next message. At the fade-complete `out→in` transition:
            #   1. Consume `on_deck` as `current_message`.
            #   2. Pick a fresh `on_deck` for the NEXT cycle.
            #   3. Stage the rotation effect (or MediaCycler
            #      override), the scroller text, and the
            #      `text_display` event log entry.
            # This is the ONLY call site for `self._pick_message_via_selector(...)`
            # in the hot path — `random.choice` (or weighted scoring)
            # runs here, once per cycle, never on a timer.
            if self._step_fade(now, fading_out=True):
                if self.on_deck is None:
                    # Defensive: pick twice — once for
                    # `current_message`, once for `on_deck` — so the
                    # very first cycle (which seeded only
                    # `on_deck` at intro→out) doesn't run with
                    # `current_message=None` immediately followed
                    # by a rotation-only background.
                    self.current_message = self._pick_message_via_selector()
                else:
                    self.current_message = self.on_deck
                # Anti-repeat: pass the just-consumed message's id to
                # the selector so the next pick won't re-pick the same
                # message back-to-back. The default selector is now
                # `WeightedSelector` (post-2026-07-18), which already
                # penalizes just-shown messages via `display_recency`
                # — the hint is a no-op for the weighted path. With
                # `RandomSelector` (the operator opt-out), the hint
                # prevents the "same message shown twice in a row"
                # symptom and its downstream cycler-suppress leak:
                # when the same MMS is re-picked, the suppress flag
                # from its cycler exhaust correctly fires (same-id
                # discriminator) and the cycler rebuild is
                # intentionally skipped, so the image fails to
                # render.
                next_exclude_id = self.current_message.id if self.current_message is not None else None
                self.on_deck = self._pick_message_via_selector(
                    exclude_id=next_exclude_id,
                )

                # Settings refresh at the cycle boundary. Tick
                # itself is a pure render — but at out→in we apply
                # the live settings from the manager: rotation
                # rebuild if the rotation list changed, scroller
                # color / speed if those changed. This is the
                # "settings refresh on the next cycle" half of
                # the architectural split.
                self._refresh_render_layer_from_settings(display, scroller)

                self.idx = (self.idx + 1) % len(effects)
                self.current = effects[self.idx]
                self.current.set_brightness(0.0)
                # MMS media override (issue #38): if the staged
                # message has a non-empty `media` list, swap a
                # MediaCycler / BrowserMediaOverlay in place of
                # the rotation effect. The cycler takes over
                # `self.current` for the hold; on `exhausted`
                # the coordinator falls back via
                # `_maybe_fall_back_to_rotation`.
                media_override = self._maybe_build_media_cycler()
                if media_override is not None:
                    self.current = media_override
                    self.current.set_brightness(0.0)

                text = self.current_message.body if self.current_message is not None else ""
                if text:
                    scroller.set_text(text, display.width)
                    scroller.set_brightness(0.0)
                else:
                    scroller.set_text("", display.width)

                log.info(
                    "Coordinator out→in: idx=%d effect=%s message_id=%s text=%r media_override=%s",
                    self.idx,
                    self.current_effect_name,
                    self.current_message.id if self.current_message is not None else "<none>",
                    text if text else "",
                    "yes" if media_override is not None else "no",
                )
                # Issue #26: write a `text_display` event to the
                # Pi-local log immediately after the picked
                # message begins rendering. The new on-deck
                # model treats fresh-id arrivals uniformly —
                # every fade-in writes a text_display event,
                # selector-driven or fresh-id. The next
                # `WeightedSelector` pick sees this and applies
                # the new `display_recency` formula
                # (just-shown → 0.0).
                if self._event_log is not None and self.current_message is not None and text:
                    try:
                        self._event_log.append(
                            {
                                "event_type": "text_display",
                                "message_id": self.current_message.id,
                                "timestamp": time.time(),
                                "received_at": self.current_message.received_at_epoch(),
                            }
                        )
                    except Exception as exc:
                        log.warning("Coordinator event-log append failed: %s", exc)
                self.mode = "in"
                self.fade_start = now
                self.last_step = 0.0

        elif mode == "in":
            if self._step_fade(now, fading_out=False):
                assert self.current is not None
                self.current.set_brightness(1.0)
                scroller.set_brightness(1.0)
                self.phase_start = now
                # `showing_text` derives from `current_message` —
                # we hold text when the message has a non-empty
                # body. Background otherwise (SMS-only message
                # with empty body; rotation effect stays put).
                next_mode = "hold" if self.showing_text else "background"
                log.info(
                    "Coordinator in→%s: effect=%s text=%r",
                    next_mode,
                    self.current_effect_name,
                    self.current_message.body if self.current_message is not None else "",
                )
                self.mode = next_mode

        elif mode == "hold":
            # MediaCycler fall-back (issue #38): if the cycler
            # exhausts mid-hold, swap to the rotation effect
            # and arm `_suppress_media_override` so the next
            # cycler rebuild returns None.
            self._maybe_fall_back_to_rotation()
            # Fresh-id pre-emption (issue #26): replace `on_deck`
            # silently. Hold runs to natural end — the new SMS
            # shows up after `hold_seconds` elapses (and the
            # post-text_out + post-background gap), not the
            # moment the SMS arrives. The cycler keeps playing
            # if it's mid-playback; a non-cycler hold is
            # uninterruptable too.
            fresh = self._fresh_id_in_buffer()
            if fresh is not None and not self._current_is_active_media_cycler():
                self.on_deck = fresh
                log.info(
                    "Coordinator hold: fresh SMS replaces on-deck (no interrupt) message_id=%s",
                    fresh.id,
                )
            if now - self.phase_start >= effects_settings.hold_seconds:
                log.info(
                    "Coordinator hold→text_out: effect=%s held_text=%r held_for=%.1fs hold_seconds=%.1f",
                    self.current_effect_name,
                    self.current_message.body if self.current_message is not None else "",
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
            # and only catch the fade on the next background tick.
            self._maybe_fall_back_to_rotation()
            # Fresh-id replacement during text_out — silent slot
            # swap, no mode transition (the text is already
            # fading; let it finish).
            fresh = self._fresh_id_in_buffer()
            if fresh is not None and not self._current_is_active_media_cycler():
                self.on_deck = fresh
            # Only the text fades; the background effect stays lit.
            if self._step_fade(now, fading_out=True, fade_effect=False):
                scroller.set_text("", display.width)
                scroller.set_brightness(1.0)
                self.phase_start = now
                # Round 4: the `text_out→background` log is dropped.
                # Same rationale as `hold→text_out` — internal
                # fade-mechanics, not a sign-state event the
                # operator needs to read.
                self.mode = "background"

        elif mode == "background":
            # MediaCycler fall-back (issue #38 follow-up): swap the
            # cycler for the rotation effect on exhaustion so the
            # idle window doesn't sit on a looping cycler frame.
            self._maybe_fall_back_to_rotation()
            # Fresh-id replacement during background — silent slot
            # swap, no immediate `_begin_out`. The next
            # background→out transition (after `IDLE_SECONDS_AFTER_HOLD`)
            # consumes whatever `on_deck` is at that moment,
            # possibly the fresh SMS that arrived mid-background.
            fresh = self._fresh_id_in_buffer()
            if fresh is not None and not self._current_is_active_media_cycler():
                self.on_deck = fresh
                log.info(
                    "Coordinator background: fresh SMS replaces on-deck message_id=%s",
                    fresh.id,
                )

            idle_elapsed = now - self.phase_start >= IDLE_SECONDS_AFTER_HOLD
            if idle_elapsed:
                log.info(
                    "Coordinator background→out (idle): waited=%.1fs idle_seconds=%.1f on_deck=%s",
                    now - self.phase_start,
                    IDLE_SECONDS_AFTER_HOLD,
                    self.on_deck.id if self.on_deck is not None else "<none>",
                )
                self._begin_out(now)  # → out → out→in consumes on_deck

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
