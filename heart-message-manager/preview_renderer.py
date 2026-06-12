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
import time

log = logging.getLogger("heart")


# Patterns that work in the browser unchanged (their dependencies are all
# in Pyodide or stdlib).
_BROWSER_COMPATIBLE_PATTERNS = ("Fireworks", "Flame", "NightSky", "Honeycomb")

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

    Cycles through `effects`, fades the active effect and scroller on each
    new message, and drives the main loop one tick at a time. The browser's
    rAF loop in static/preview.js calls `tick()` at the device's frame
    cadence.
    """

    def __init__(
        self, display, scroller, effects, fade_seconds=4.0, fade_step=0.04, gamma=2.2
    ):
        self.display = display
        self.scroller = scroller
        self.effects = effects
        self.idx = 0
        self.fade_seconds = fade_seconds
        self.fade_step = fade_step
        self.gamma = gamma
        self.mode = "idle"  # idle | out | in
        self.fade_start = 0.0
        self.last_step = 0.0
        self.pending_text = None
        self._last_text = ""  # tracks the most recently handed-off body

    def request_message(self, text):
        """Hand a new body to the coordinator.

        The body is held until the current fade-out completes, at which point
        the active effect index advances, the scroller is given the new text,
        and a fade-in begins. Empty / None / duplicate bodies are ignored.
        """
        if not text:
            return
        if text == self._last_text:
            return
        self._last_text = text
        self.pending_text = text
        self.mode = "out"
        self.fade_start = time.monotonic()
        self.last_step = 0.0

    @property
    def current_effect_name(self):
        """The class name of the active effect — used by the status block."""
        if not self.effects:
            return "—"
        return type(self.effects[self.idx]).__name__

    @property
    def current_text(self):
        """The body of the message currently being scrolled (or '' for idle)."""
        return self.scroller.text or ""

    def tick(self):
        """Advance one frame.

        If a fade is in progress, step the brightness. Once the fade-out
        completes, advance the effect index, hand the pending text to the
        scroller, and switch to fade-in. The active effect's tick() runs
        every frame (so it animates during fades), then the scroller, then
        composite onto the WebCanvas.
        """
        now = time.monotonic()
        if self.mode != "idle":
            progress = (now - self.fade_start) / self.fade_seconds
            if progress > 1.0:
                progress = 1.0

            if now - self.last_step >= self.fade_step or progress >= 1.0:
                self.last_step = now
                linear = 1.0 - progress if self.mode == "out" else progress
                b = linear**self.gamma
                self.effects[self.idx].set_brightness(b)
                self.scroller.set_brightness(b)
                log.debug("fade %s linear=%.3f b=%.3f", self.mode, linear, b)

            if progress >= 1.0:
                if self.mode == "out":
                    self.idx = (self.idx + 1) % len(self.effects)
                    self.effects[self.idx].set_brightness(0.0)
                    self.scroller.set_text(self.pending_text, self.display.width)
                    self.pending_text = None
                    self.mode = "in"
                    self.fade_start = now
                    self.last_step = 0.0
                else:  # "in" complete
                    self.effects[self.idx].set_brightness(1.0)
                    self.scroller.set_brightness(1.0)
                    self.mode = "idle"

        # Always tick the active effect and scroller (they animate during fades)
        self.effects[self.idx].tick()
        self.scroller.tick(self.display.width)

        # Composite: clear the frame, draw the effect, draw the scroller.
        # Each Effect subclass's render() expects a clean canvas to avoid
        # ghosting from the previous frame.
        self.display.canvas.clear()
        self.effects[self.idx].render(self.display.canvas)
        self.scroller.render(self.display.canvas)
