"""Browser-side coordinator and effect cycle for the sign preview.

Mirrors the device's `EffectCoordinator` from heart-matrix-controller/main.py:
toggle between effects, fade brightness in/out on each new message, and
drive the scroller. Runs in the browser via PyScript, so the per-frame
loop stays in the user's tab rather than on the Flask server.

Effect cycle wiring (Section 4): each pattern's constructor is wrapped in
a try/except. A constructor that raises (missing assets, missing optional
deps) is logged once and excluded from the cycle. PngDisplay and
VideoDisplay are explicitly skipped in v1 — they need filesystem assets
the browser can't read (and VideoDisplay needs OpenCV, which is not in
Pyodide).
"""

import logging
import random
import time
from collections import deque

log = logging.getLogger("heart")


# Rotation effects that work in the browser unchanged (their dependencies are
# all in Pyodide or stdlib). Order mirrors the device's cycle in
# heart-matrix-controller/main.py, minus PngDisplay / VideoDisplay (skipped).
_BROWSER_COMPATIBLE_PATTERNS = ("Hyperspace", "Honeycomb", "Flame", "Fireworks", "NightSky")

# Patterns we explicitly skip in v1, with the reason logged for operators.
_BROWSER_SKIPPED_PATTERNS = {
    "PngDisplay": "needs design/pngs/* on the filesystem",
    "VideoDisplay": "needs OpenCV (cv2) — not in Pyodide",
}


class PreviewRenderer:
    """Build the effect cycle, skipping constructors that raise.

    Each pattern is instantiated inside a try/except. A failure is logged
    once with the pattern name and reason; the pattern is excluded from
    the cycle. Patterns listed in _BROWSER_SKIPPED_PATTERNS are also
    excluded with a logged reason, so the behavior is consistent whether
    the constructor raises or the pattern is just unsupported.
    """

    def __init__(self, display, patterns_module):
        """Args:
        display: a WebDisplay instance.
        patterns_module: the heart-matrix-controller.patterns module
            (imported lazily by the browser; we receive the module
            object so we can introspect its classes by name).
        """
        self.display = display
        self.effects = []
        self._init_effects(patterns_module)

    def _init_effects(self, patterns_module):
        for name in _BROWSER_COMPATIBLE_PATTERNS:
            # The patterns package's __init__.py is empty, so the Effect
            # subclasses live one level down (e.g. patterns.fireworks.Fireworks)
            # — the device's main.py imports them with `from patterns.fireworks
            # import Fireworks`. We mirror that here: first try
            # `patterns.<name>` for symmetry, then fall back to
            # `patterns.<lowercase name>.<Name>`.
            cls = getattr(patterns_module, name, None)
            if cls is None:
                submodule = getattr(patterns_module, name.lower(), None)
                if submodule is not None:
                    cls = getattr(submodule, name, None)
            if cls is None:
                log.warning("Pattern %s not found in patterns module — skipping", name)
                continue
            try:
                self.effects.append(cls(self.display))
            except Exception as e:
                log.warning(
                    "Pattern %s failed to initialize in browser (%s: %s) — skipping",
                    name,
                    type(e).__name__,
                    e,
                )
        for name, reason in _BROWSER_SKIPPED_PATTERNS.items():
            log.info("Pattern %s skipped in browser preview (%s)", name, reason)


class PreviewCoordinator:
    """Mirrors heart-matrix-controller/main.py:EffectCoordinator for the browser.

    Same lifecycle state machine as the device:

        intro    — a beating heart for `intro_seconds`, no text.
        out      — cross-fade the current effect+text to black.
        in       — fade the next effect (+ message text) up.
        hold     — keep a message fully visible for `hold_seconds`.
        text_out — fade only the text out, leaving the background effect lit.
        background — just the effect, no text, until the next message.

    A new message (handed in by the preview.js poll via `request_message`)
    interrupts `hold`/`background`. After `idle_seconds` with nothing new, a
    random one of the last few bodies the coordinator has seen is shown, so the
    preview keeps moving like the sign does. The browser keeps its own recent-
    body buffer (the device reads the MessageManager); everything else matches.

    The browser's rAF loop in static/preview.js calls `tick()` at the device's
    frame cadence.
    """

    def __init__(
        self,
        display,
        scroller,
        effects,
        heart,
        fade_seconds=2.0,
        hold_seconds=15.0,
        intro_seconds=5.0,
        idle_seconds=300.0,
        recent_count=5,
        fade_step=0.04,
        gamma=2.2,
    ):
        self.display = display
        self.scroller = scroller
        self.effects = effects
        self.heart = heart
        self.fade_seconds = fade_seconds
        self.hold_seconds = hold_seconds
        self.intro_seconds = intro_seconds
        self.idle_seconds = idle_seconds
        self.fade_step = fade_step
        self.gamma = gamma

        self.idx = -1  # first message shown advances this to 0
        self.current = heart  # effect being rendered right now
        self.mode = "intro"
        self.fade_start = 0.0
        self.last_step = 0.0
        self.phase_start = time.monotonic()  # start of intro / hold / background
        self.pending_text = None
        self.showing_text = False
        self.last_shown_text = None
        # The browser's stand-in for the device's message history: the last few
        # bodies handed in via request_message, used for the idle random pick.
        self._recent = deque(maxlen=recent_count)

    def start(self, startup_text=None):
        """(Re)begin the boot splash, queuing a message to show after the heart."""
        if startup_text:
            self.pending_text = startup_text
            if startup_text not in self._recent:
                self._recent.append(startup_text)
        self.current = self.heart
        self.heart.set_brightness(1.0)
        self.mode = "intro"
        self.phase_start = time.monotonic()

    def request_message(self, text):
        """Queue a freshly-arrived body; shown at the next stable point.

        Empty / None bodies are ignored. preview.js already dedupes against the
        last body it handed in, so a non-empty text here is genuinely new.
        """
        if not text:
            return
        self.pending_text = text
        if not self._recent or self._recent[-1] != text:
            self._recent.append(text)

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
        """A random body from the recent buffer (avoid repeating the last one)."""
        bodies = [b for b in self._recent if b]
        if not bodies:
            return None
        choices = [b for b in bodies if b != self.last_shown_text] or bodies
        return random.choice(choices)

    @property
    def current_effect_name(self):
        """The class name of the active effect — used by the status block."""
        return type(self.current).__name__

    @property
    def current_text(self):
        """The body of the message currently being scrolled (or '' for idle)."""
        return self.scroller.text or ""

    def tick(self):
        """Advance one frame through the lifecycle, then composite the canvas."""
        now = time.monotonic()
        mode = self.mode

        if mode == "intro":
            if now - self.phase_start >= self.intro_seconds:
                self._begin_out(now)

        elif mode == "out":
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
                self._begin_out(now)  # a new message interrupts the hold
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

        # Always tick the active effect + scroller (they animate during fades).
        self.current.tick()
        self.scroller.tick(self.display.width)

        # Composite: clear the frame, draw the effect, draw the scroller.
        # Each Effect subclass's render() expects a clean canvas to avoid
        # ghosting from the previous frame.
        self.display.canvas.clear()
        self.current.render(self.display.canvas)
        self.scroller.render(self.display.canvas)
