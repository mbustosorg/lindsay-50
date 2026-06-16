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

The Pi hands the coordinator a `recent_provider` callable (e.g. one that
queries the `MessageManager` for the last few bodies). The browser doesn't
have that — it relies on an internal `deque` populated by `set_text`.
The class supports both via the optional `recent_provider` argument.

Pacing + recent_count come from an `EffectsSettings` block (the v2 config
shape), passed as `settings` (an `EffectsSettings` instance or `None` for
the historic per-kwarg defaults).
"""

import logging
import random
import time
from collections import deque

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

    Used directly by both the Pi entrypoint and the browser preview; no
    subclass. The Pi passes a `recent_provider` callable that reads from
    its message store; the browser omits it and the coordinator uses its
    own internal deque of bodies handed in via `set_text`.

    Args:
        display: a `DisplayBase` subclass (`MatrixDisplay` on the Pi,
            `WebDisplay` in the browser).
        scroller: a scroller with `tick(width)`, `render(canvas)`,
            `set_text(text, width)`, and `set_brightness(b)`.
        effects: list of `Effect` instances for the rotation cycle.
        heart: the boot-splash effect (shown at intro).
        recent_provider: optional callable returning recent message entries
            (each with a `.message.body` attribute) for the idle random
            pick. If `None`, the coordinator uses its internal `_recent`
            deque populated by `set_text`.
        settings: optional `EffectsSettings` instance supplying
            `fade_seconds`, `hold_seconds`, `intro_seconds`,
            `idle_seconds`, and `recent_count`. When omitted, defaults
            are used.
        fade_seconds: seconds for one full fade (used when `settings` is None).
        hold_seconds: seconds to keep a message fully visible (used when
            `settings` is None).
        intro_seconds: seconds to show the boot-splash heart (used when
            `settings` is None).
        idle_seconds: seconds of idleness before a random message plays
            (used when `settings` is None).
        recent_count: size of the internal recent-messages deque (used when
            `settings` is None).
        fade_step: throttle (seconds) between palette writes during a fade.
        gamma: gamma exponent applied to the linear fade progress.
    """

    def __init__(
        self,
        display,
        scroller,
        effects,
        heart,
        recent_provider=None,
        settings: EffectsSettings | None = None,
        fade_seconds: float = 2.0,
        hold_seconds: float = 15.0,
        intro_seconds: float = 5.0,
        idle_seconds: float = 300.0,
        recent_count: int = 5,
        fade_step: float = 0.04,
        gamma: float = 2.2,
    ):
        self.display = display
        self.scroller = scroller
        self.effects = effects
        self.heart = heart
        self.recent_provider = recent_provider
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
        self.current = heart  # effect being rendered right now
        self.mode = "intro"
        self.fade_start = 0.0
        self.last_step = 0.0
        self.phase_start = 0.0  # start of intro / hold / background
        self.pending_text = None  # next message body to show (None = nothing)
        self.showing_text = False
        self.last_shown_text = None
        # Browser-side recent-message buffer. The Pi uses `recent_provider`
        # instead, so this deque is never consulted when a provider is set.
        self._recent: deque = deque(maxlen=self.recent_count)

    def start(self, startup_text):
        """Begin the boot splash, queuing the seeded message to show after it."""
        if startup_text:
            self.pending_text = startup_text
            if self.recent_provider is None and startup_text not in self._recent:
                self._recent.append(startup_text)
        self.current = self.heart
        self.heart.set_brightness(1.0)
        self.mode = "intro"
        self.phase_start = time.monotonic()

    def set_text(self, text):
        """Queue a freshly-arrived message; shown at the next stable point.

        Empty / None bodies are ignored. When no `recent_provider` is set
        (browser path), the body is also appended to the internal deque
        (deduped against the most recent entry) so the idle random pick has
        material to choose from.
        """
        if not text:
            return
        self.pending_text = text
        if self.recent_provider is None:
            if not self._recent or self._recent[-1] != text:
                self._recent.append(text)

    # Backwards-compat alias for the pre-v2 entrypoint. New code calls
    # `set_text`; existing tests still call `request_message`.
    def request_message(self, text):
        """Deprecated alias for `set_text`; kept so old call sites compile."""
        self.set_text(text)

    def apply_settings(self, effect_settings: EffectsSettings) -> None:
        """Live-update pacing + recent_count from a v2 `EffectsSettings`.

        Called when a config envelope arrives over MQTT/WS; mutates
        the coordinator's pacing attributes in place. Does NOT touch
        the effects rotation — that's a separate `build_effects` call
        that the caller (Pi main.py / preview_main.py) is expected to
        make, then assign to `coordinator.effects`.
        """
        if effect_settings is None:
            return
        self.fade_seconds = effect_settings.fade_seconds
        self.hold_seconds = effect_settings.hold_seconds
        self.intro_seconds = effect_settings.intro_seconds
        self.idle_seconds = effect_settings.idle_seconds
        self.recent_count = effect_settings.recent_count
        # Resize the in-memory recent deque. Existing entries are kept
        # up to the new maxlen; older ones are dropped automatically.
        # Always rebuild — the deque is small (max ~dozens of bodies)
        # and the test suite expects `coord._recent.maxlen` to track
        # `recent_count` even when a `recent_provider` is configured
        # (the deque is unused in that case but must stay consistent).
        existing = list(self._recent)
        self._recent = deque(existing, maxlen=self.recent_count)

    def _step_fade(self, now, fading_out, fade_effect=True, fade_text=True):
        """Advance the active fade one throttled step; return True when complete."""
        progress = (now - self.fade_start) / self.fade_seconds
        if progress > 1.0:
            progress = 1.0
        if now - self.last_step >= self.fade_step or progress >= 1.0:
            self.last_step = now
            linear = (1.0 - progress) if fading_out else progress
            b = linear**self.gamma
            if fade_effect:
                self.current.set_brightness(b)
            if fade_text:
                self.scroller.set_brightness(b)
        return progress >= 1.0

    def _begin_out(self, now):
        self.mode = "out"
        self.fade_start = now
        self.last_step = 0.0

    def _random_recent(self):
        """A random body from the last `recent_count` messages (avoid repeat)."""
        if self.recent_provider is not None:
            try:
                entries = self.recent_provider() or []
            except Exception:
                log.exception("recent_provider failed")
                return None
            bodies = [e.message.body for e in entries[: self.recent_count] if e.message.body]
        else:
            bodies = [b for b in self._recent if b]
        if not bodies:
            return None
        choices = [b for b in bodies if b != self.last_shown_text] or bodies
        return random.choice(choices)

    @property
    def current_effect_name(self):
        """The class name of the active effect (browser status block)."""
        return type(self.current).__name__

    @property
    def current_text(self):
        """The body of the message currently being scrolled (or '' for idle)."""
        return self.scroller.text or ""

    def tick(self):
        now = time.monotonic()
        mode = self.mode

        if mode == "intro":
            if now - self.phase_start >= self.intro_seconds:
                self._begin_out(now)

        elif mode == "out":
            # Cross-fade the current effect + any text to black, then swap in
            # the next effect and (if queued) the next message.
            if self._step_fade(now, fading_out=True):
                self.idx = (self.idx + 1) % len(self.effects)
                self.current = self.effects[self.idx]
                self.current.set_brightness(0.0)
                text = self.pending_text
                self.pending_text = None
                if text:
                    self.scroller.set_text(text, self.display.width)
                    self.scroller.set_brightness(0.0)
                    self.showing_text = True
                    self.last_shown_text = text
                else:
                    self.scroller.set_text("", self.display.width)
                    self.showing_text = False
                self.mode = "in"
                self.fade_start = now
                self.last_step = 0.0

        elif mode == "in":
            if self._step_fade(now, fading_out=False):
                self.current.set_brightness(1.0)
                self.scroller.set_brightness(1.0)
                self.phase_start = now
                self.mode = "hold" if self.showing_text else "background"

        elif mode == "hold":
            if self.pending_text is not None:
                self._begin_out(now)  # new SMS interrupts the hold
            elif now - self.phase_start >= self.hold_seconds:
                self.mode = "text_out"
                self.fade_start = now
                self.last_step = 0.0

        elif mode == "text_out":
            # Only the text fades; the background effect stays lit.
            if self._step_fade(now, fading_out=True, fade_effect=False):
                self.scroller.set_text("", self.display.width)
                self.scroller.set_brightness(1.0)
                self.showing_text = False
                self.phase_start = now
                self.mode = "background"

        elif mode == "background":
            if self.pending_text is not None:
                self._begin_out(now)  # show the queued message
            elif now - self.phase_start >= self.idle_seconds:
                text = self._random_recent()
                if text:
                    self.pending_text = text
                    self._begin_out(now)
                else:
                    self.phase_start = now  # nothing to show; reset the timer

        self.current.tick()
        self.scroller.tick(self.display.width)
        # Composite the active effect + scroller onto the panel. The display
        # owns the clear/draw/swap sequence (and, on the Pi, the SwapOnVSync
        # pacing).
        self.display.render(self.current, self.scroller)
