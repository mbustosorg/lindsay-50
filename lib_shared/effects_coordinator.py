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
`message_manager` is a required constructor argument. `tick()` throttles a
~4 Hz pull from `manager.messages.get_messages(...)` and the cached pull
result becomes the next text shown by the fade state machine.

Pacing comes from an `EffectsSettings` block (the v2 config shape),
passed as `settings` (an `EffectsSettings` instance or `None` for
the historic per-kwarg defaults).
"""

import logging
import random
import time

from lib_shared.models import EffectsSettings

log = logging.getLogger("heart")


def build_effects(
    effect_settings: EffectsSettings,
    effect_class_factory=None,
    *,
    display=None,
) -> list:
    """Build the effects rotation from a v2 `EffectsSettings` block.

    Iterates `effect_settings.effects` in declared order. For each
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
        effect_settings: The v2 `EffectsSettings` block from `SignConfig`,
            or None (returns [] — there's no settings to derive a
            fallback from).
        effect_class_factory: Callable `name -> type | None`. Defaults
            to `lib_shared.effects_factory.make_effect_class`. Tests
            can pass a stub that returns simple effect classes.
        display: The display object handed to each Effect's constructor.
            Required when `effect_settings` is not None — every Effect
            subclass needs a display.

    Returns:
        A list of instantiated Effect objects in the order
        declared in `effect_settings.effects`. Enabled effects
        only. Empty when `effect_settings is None` or when the
        fallback is also unresolvable.
    """
    if effect_settings is None:
        return []
    if effect_class_factory is None:
        from lib_shared.effects_factory import make_effect_class

        effect_class_factory = make_effect_class
    if display is None:
        raise ValueError("build_effects requires `display` to instantiate effects")
    out = []
    for entry in effect_settings.effects:
        if not entry.get("enabled"):
            continue
        cls = effect_class_factory(entry.get("name", ""))
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
        for entry in effect_settings.effects:
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
        settings: optional `EffectsSettings` instance supplying
            `fade_seconds`, `hold_seconds`, `intro_seconds`,
            `idle_seconds`, and `recent_count`. When omitted, defaults
            are used.
        fade_seconds: seconds for one full fade (used when `settings` is None).
        hold_seconds: seconds to keep a message fully visible (used when
            `settings` is None).
        intro_seconds: seconds to show the boot-splash heart (used when
            `settings` is None).
        idle_seconds: seconds of idleness before... no longer used for
            a random pick (the in-memory deque is gone) but kept on
            the dataclass for compat with the historic per-kwarg
            defaults.
        recent_count: retained on the dataclass for compat with the
            historic per-kwarg defaults; no longer used at runtime
            (no in-memory recent deque, no `recent_provider`).
        fade_step: throttle (seconds) between palette writes during a fade.
        gamma: gamma exponent applied to the linear fade progress.
    """

    # Throttle the coordinator's pull from MessageManager.messages. 4 Hz is
    # 4x faster than any human perceives text change and far below the 30+
    # FPS cost we are avoiding.
    _PULL_INTERVAL = 0.25

    def __init__(
        self,
        message_manager,
        display=None,
        scroller=None,
        effects=None,
        heart=None,
        settings: EffectsSettings | None = None,
        fade_seconds: float = 2.0,
        hold_seconds: float = 15.0,
        intro_seconds: float = 5.0,
        idle_seconds: float = 300.0,
        recent_count: int = 5,
        fade_step: float = 0.04,
        gamma: float = 2.2,
    ):
        # Required — no default. Raises TypeError if a caller omits it.
        self.message_manager = message_manager
        self.display = display
        self.scroller = scroller
        self.effects = list(effects) if effects is not None else []
        self.heart = heart
        if settings is not None:
            self.fade_seconds = settings.fade_seconds
            self.hold_seconds = settings.hold_seconds
            self.intro_seconds = settings.intro_seconds
            self.idle_seconds = settings.idle_seconds
            self.recent_count = settings.recent_count
        else:
            self.fade_seconds = fade_seconds
            self.hold_seconds = hold_seconds
            self.intro_seconds = intro_seconds
            self.idle_seconds = idle_seconds
            self.recent_count = recent_count
        # Throttles palette writes during a fade. Without this, a fast main loop
        # rewrites the palette far faster than the panel refreshes, wasting work.
        self.fade_step = fade_step
        # Gamma correction: linear time → perceptually linear brightness.
        self.gamma = gamma

        self.idx = -1  # first message shown advances this to 0
        self.current = heart  # effect being rendered right now (may be None)
        self.mode = "intro"
        self.fade_start = 0.0
        self.last_step = 0.0
        self.phase_start = 0.0  # start of intro / hold / background
        self.showing_text = False
        self.last_shown_text = None
        # Pull-throttle state. `_last_message_pull` is the monotonic time of
        # the last pull from the manager; `_last_display_message` is the
        # cached body that non-pull ticks consume.
        self._last_message_pull: float = 0.0
        self._last_display_message: str | None = None
        self._last_shown_message_id: str | None = None
        # Hashes that guard the heavier config-application work
        # (effects rebuild, scroller text settings mutation) in
        # `apply_settings`. `None` means "never applied" so the first
        # apply always runs the heavier work.
        self._last_effects_hash: str | None = None
        self._last_text_settings_hash: str | None = None

    def is_bound(self) -> bool:
        """True when the coordinator has a render layer (display + scroller + effects + heart).

        An unbound coordinator is a no-op: `tick()` returns without
        touching state. The Pi always binds before its first tick
        (it constructs the display + scroller + effects in the
        same module-level pass). The browser's `app_main.py`
        instantiates the coordinator unbound; the preview page
        binds once the canvas is in scope.
        """
        return self.display is not None and self.scroller is not None and bool(self.effects) and self.heart is not None

    def bind(
        self,
        display,
        scroller,
        effects,
        heart=None,
        effect_settings: EffectsSettings | None = None,
        text_settings=None,
    ) -> None:
        """Attach (or swap) the render layer.

        Replaces `display`/`scroller`/`effects`/`heart` in place.
        `heart` defaults to the head of the new effects list when
        not supplied (preview uses the first effect as the
        boot-splash; the Pi passes an explicit Heartbeat instance).

        The app-scoped coordinator is constructed unbound (no
        render layer) by `app_main.py`; the manager's `on_change`
        callback fires `apply_settings` on it before the preview
        has had a chance to `bind`. `apply_settings` is therefore
        defensive about a missing `display` — pacing fields are
        still written (cheap, harmless pre-bind) but the
        effects rebuild and the scroller mutation are skipped.
        To avoid a stale pre-bind rotation hanging around after
        the preview finally binds, the caller can pass the
        current `effect_settings` and `text_settings` here; we
        forward them through `apply_settings` to refresh the
        render layer with the manager's current config.

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
        if effect_settings is not None:
            self.apply_settings(effect_settings, text_settings)

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

    def get_display_message(self) -> str | None:
        """Pick the body to display next, from the manager's buffered messages.

        Algorithm:
          1. Read `self.recent_count` most-recent non-suppressed messages.
          2. If the list is empty, return None.
          3. If the head entry's id differs from `self._last_shown_message_id`,
             return its body and update `_last_shown_message_id` (fresh-message
             priority).
          4. Otherwise pick uniformly at random from the list and return that
             entry's body, updating `_last_shown_message_id` to the picked id.

        Returns:
            The body string to show next, or None when the buffer is empty.
        """
        entries = self.message_manager.messages.get_messages(limit=self.recent_count, suppress=True)
        if not entries:
            return None
        head = entries[0]
        if head.message.id != self._last_shown_message_id:
            self._last_shown_message_id = head.message.id
            return head.message.body
        picked = random.choice(entries)
        self._last_shown_message_id = picked.message.id
        return picked.message.body

    def apply_settings(
        self,
        effect_settings: EffectsSettings,
        text_settings=None,
    ) -> None:
        """Live-update pacing + effects rotation + scroller text settings.

        Called by `MessageManager`'s `on_change` callback (the single
        Python-side fan-out point). Idempotent on message-only emits:
        pacing writes always run (cheap), but the effects rebuild
        and scroller text-settings mutation are guarded by a hash of
        the relevant fields, so they only run on actual config
        changes.

        Args:
            effect_settings: v2 effects block (pacing + rotation).
            text_settings: v2 text block (color, speed). Optional
                for callers that only have effects; when omitted,
                the scroller text settings are not touched.
        """
        if effect_settings is not None:
            self.fade_seconds = effect_settings.fade_seconds
            self.hold_seconds = effect_settings.hold_seconds
            self.intro_seconds = effect_settings.intro_seconds
            self.idle_seconds = effect_settings.idle_seconds
            self.recent_count = effect_settings.recent_count

            # Effects rebuild is guarded by a hash of the declared rotation.
            # When the coordinator is unbound (`self.display is None`) we
            # still write the pacing fields above but defer the rebuild —
            # `build_effects` requires a `display` to instantiate effects
            # and the app-scoped coordinator's `on_change` callback fires
            # `apply_settings` before `bind()` has a chance to run on the
            # preview page. The hashes are NOT updated in the unbound
            # case, so the first `apply_settings` after `bind()` re-runs
            # the heavier work with the now-present display.
            effects_hash = self._hash_effects(effect_settings.effects)
            if effects_hash != self._last_effects_hash and self.display is not None:
                new_effects = build_effects(effect_settings, display=self.display)
                self.effects = new_effects
                self.idx = -1  # next fade picks the head of the new list
                self._last_effects_hash = effects_hash

        if text_settings is not None and self.scroller is not None:
            text_hash = self._hash_text_settings(text_settings)
            if text_hash != self._last_text_settings_hash:
                self.scroller.set_color(text_settings.color)
                self.scroller.set_speed(text_settings.speed)
                self._last_text_settings_hash = text_hash

    @staticmethod
    def _hash_effects(effects) -> str:
        """Stable hash of an effects rotation list (name + enabled per entry)."""
        return repr([(e.get("name"), e.get("enabled")) for e in (effects or [])])

    @staticmethod
    def _hash_text_settings(text_settings) -> str:
        """Stable hash of the text-settings fields the scroller consumes."""
        return f"({text_settings.color!r},{text_settings.speed!r})"

    def _step_fade(self, now, fading_out, fade_effect=True, fade_text=True):
        """Advance the active fade one throttled step; return True when complete."""
        progress = (now - self.fade_start) / self.fade_seconds
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
        """
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
        now = time.monotonic()
        mode = self.mode

        # Throttled pull: only fetch a fresh body every _PULL_INTERVAL.
        # The cached value drives the state-machine transitions.
        if now - self._last_message_pull >= self._PULL_INTERVAL:
            self._last_display_message = self.get_display_message()
            self._last_message_pull = now
        text = self._last_display_message

        if mode == "intro":
            if now - self.phase_start >= self.intro_seconds:
                self._begin_out(now)

        elif mode == "out":
            # Cross-fade the current effect + any text to black, then swap in
            # the next effect and (if there's text) the next message.
            if self._step_fade(now, fading_out=True):
                self.idx = (self.idx + 1) % len(effects)
                self.current = effects[self.idx]
                self.current.set_brightness(0.0)
                if text:
                    scroller.set_text(text, display.width)
                    scroller.set_brightness(0.0)
                    self.showing_text = True
                    self.last_shown_text = text
                else:
                    scroller.set_text("", display.width)
                    self.showing_text = False
                self.mode = "in"
                self.fade_start = now
                self.last_step = 0.0

        elif mode == "in":
            if self._step_fade(now, fading_out=False):
                assert self.current is not None
                self.current.set_brightness(1.0)
                scroller.set_brightness(1.0)
                self.phase_start = now
                self.mode = "hold" if self.showing_text else "background"

        elif mode == "hold":
            # A fresh message (one whose id differs from the last we showed)
            # interrupts the hold. We compare against `_last_shown_message_id`,
            # which `get_display_message` updates on every pick. Use the
            # pulled `text` to detect "a new pick since we last consumed one"
            # by checking that the pull produced a different body than we
            # already have on screen.
            if text and text != self.last_shown_text:
                self._begin_out(now)  # new SMS interrupts the hold
            elif now - self.phase_start >= self.hold_seconds:
                self.mode = "text_out"
                self.fade_start = now
                self.last_step = 0.0

        elif mode == "text_out":
            # Only the text fades; the background effect stays lit.
            if self._step_fade(now, fading_out=True, fade_effect=False):
                scroller.set_text("", display.width)
                scroller.set_brightness(1.0)
                self.showing_text = False
                self.phase_start = now
                self.mode = "background"

        elif mode == "background":
            # A new pull (different body than we last showed) kicks a fade.
            if text and text != self.last_shown_text:
                self._begin_out(now)  # show the queued message

        current = self.current
        assert current is not None
        current.tick()
        scroller.tick(display.width)
        # Composite the active effect + scroller onto the panel. The display
        # owns the clear/draw/swap sequence (and, on the Pi, the SwapOnVSync
        # pacing).
        display.render(current, scroller)
