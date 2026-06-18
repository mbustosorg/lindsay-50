"""Shared scroller time/pixel logic for the LED panel and the browser preview.

The Raspberry Pi display renders text via `rgbmatrix.graphics.DrawText`; the
browser preview renders text via Pillow. The two environments differ in how
they load fonts and blit glyphs, but the *behavior* — when a frame is ready,
how many pixels to advance, the two-line offset — is the same.

`ScrollerBase` owns that shared state and math. Subclasses implement three
hooks: `measure_text` (pixel width of a string in the loaded font),
`draw_text` (blit a string at a given (x, y) with a given color), and
`compute_layout` (set the top_y / bottom_y baselines and the single_line flag
for the canvas size).
"""

import logging
import time

log = logging.getLogger("heart")


class ScrollerBase:
    """Scroller time/pixel logic shared by MatrixScroller (rgbmatrix) and
    PreviewScroller (Pillow). Subclasses implement font loading and drawing.

    The user-facing `speed` kwarg (1..5) is the canonical input; the
    underlying `frame_delay` / `offset_seconds` are derived from
    `SPEED_TABLE` inside the constructor and stored as instance
    attributes (so `set_speed()` can update them in place). Tests that
    need a non-default pacing either pass `speed=` or assign the
    attributes directly after construction.
    """

    # Speed 1..5 → (frame_delay, offset_seconds). Index 0 = speed 1.
    # frame_delay shrinks as speed grows; offset_seconds shrinks mildly
    # so the two-line offset feels tight at high speed.
    SPEED_TABLE: tuple[tuple[float, float], ...] = (
        (0.080, 1.5),  # 1 — Low
        (0.060, 1.2),  # 2
        (0.040, 1.0),  # 3 — Medium  (default)
        (0.030, 0.8),  # 4
        (0.020, 0.5),  # 5 — High
    )
    SPEED_LABELS: tuple[str, ...] = (
        "1-Low",
        "2",
        "3-Medium",
        "4",
        "5-High",
    )
    DEFAULT_SPEED = 3

    @classmethod
    def resolve_pacing(cls, speed: int) -> tuple[float, float]:
        """Translate a 1..5 speed knob to (frame_delay, offset_seconds).

        Raises ValueError on out-of-range, non-int, or bool input (bool is
        an int subclass in Python and must be rejected explicitly).
        """
        if isinstance(speed, bool) or not isinstance(speed, int) or not 1 <= speed <= 5:
            raise ValueError(f"speed must be an integer in 1..5, got {speed!r}")
        return cls.SPEED_TABLE[speed - 1]

    def __init__(
        self,
        *,
        speed: int = DEFAULT_SPEED,
        color: int = 0xFF0000,
    ):
        # Translate speed → pacing
        self.frame_delay, self.offset_seconds = self.resolve_pacing(speed)
        self.text = ""
        self.text_width = 0
        self.start_time = 0.0
        self.last_frame = 0.0
        self.top_x = 0
        self.bottom_x = 0
        self.single_line = False
        self.top_y = 0
        self.bottom_y = 0
        self._color = color
        self._brightness = 1.0
        # Subclass must populate after font load:
        #   self.font, self.font_height, self.font_baseline

    def set_brightness(self, b):
        self._brightness = b

    def set_color(self, color: int) -> None:
        """Live-update the text color (used by config-envelope handlers)."""
        self._color = color

    def set_speed(self, speed: int) -> None:
        """Live-update the pacing (frame_delay + offset_seconds) from a
        1..5 speed knob. Validates the same way the constructor does."""
        fd, off = self.resolve_pacing(speed)
        self.frame_delay = fd
        self.offset_seconds = off

    def color_tuple(self):
        c, b = self._color, self._brightness
        return (
            int(((c >> 16) & 0xFF) * b),
            int(((c >> 8) & 0xFF) * b),
            int((c & 0xFF) * b),
        )

    def set_text(self, text, canvas_width):
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        self.text = str(text)
        self.text_width = self.measure_text(self.text)
        self.top_x = canvas_width
        self.bottom_x = canvas_width
        now = time.monotonic()
        self.start_time = now
        self.last_frame = now
        log.debug("New scroll text: %r", self.text)

    def tick(self, canvas_width):
        if not self.text:
            return
        now = time.monotonic()
        elapsed = now - self.last_frame
        if elapsed < self.frame_delay:
            return
        # Move one pixel per frame_delay's worth of elapsed time, so a slow
        # main-loop iteration produces a multi-pixel catch-up rather than a
        # delayed single step.
        pixels = int(elapsed / self.frame_delay)
        self.last_frame += pixels * self.frame_delay

        end_x = -self.text_width
        new_top = self.top_x - pixels
        if new_top < end_x:
            new_top = canvas_width
        self.top_x = new_top
        if not self.single_line and now - self.start_time >= self.offset_seconds:
            new_bot = self.bottom_x - pixels
            if new_bot < end_x:
                new_bot = canvas_width
            self.bottom_x = new_bot

    def render(self, canvas):
        if not self.text:
            return
        color = self.color_tuple()
        self.draw_text(canvas, self.text, self.top_x, self.top_y, color)
        if not self.single_line:
            self.draw_text(canvas, self.text, self.bottom_x, self.bottom_y, color)

    # --- Subclass hooks ---

    def measure_text(self, text):
        """Subclass: return the pixel width of `text` in self.font."""
        raise NotImplementedError

    def draw_text(self, canvas, text, x, y, color):
        """Subclass: blit `text` at (x, y) in self.font using the given color."""
        raise NotImplementedError

    def compute_layout(self, canvas_width, canvas_height):
        """Subclass: set self.single_line, self.top_y, self.bottom_y, and
        initial x positions from the canvas size and font metrics."""
        raise NotImplementedError
