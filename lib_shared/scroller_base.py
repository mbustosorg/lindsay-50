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
    PreviewScroller (Pillow). Subclasses implement font loading and drawing."""

    def __init__(self, frame_delay=0.04, offset_seconds=1.0, color=0xFF0000):
        self.frame_delay = frame_delay
        self.offset_seconds = offset_seconds
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
