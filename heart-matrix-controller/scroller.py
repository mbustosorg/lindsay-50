"""Scrolling text, rendered with the hzeller rpi-rgb-led-matrix graphics API.

Replaces the displayio Label + terminalio.FONT approach. Two copies of the
message scroll right-to-left, one centered in each 64x32 panel, the lower one
lagging by `offset_seconds`. The font is a BDF loaded from FONT_PATH (copy one
from the rpi-rgb-led-matrix `fonts/` directory).
"""

import time
import logging
from rgbmatrix import graphics

from lib_shared.config_reader import get_config

log = logging.getLogger("heart")


class Scroller:
    def __init__(self, display, color=0xFF0000, frame_delay=0.04, offset_seconds=1.0,
                 font_path=None):
        self.display = display
        self.frame_delay = frame_delay
        self.offset_seconds = offset_seconds
        self.last_frame = 0.0
        self.start_time = 0.0
        self.text_width = 0
        self._color = color
        self._brightness = 1.0

        cfg = get_config()
        path = font_path or cfg.if_exists("FONT_PATH") or "fonts/8x13B.bdf"
        self.font = graphics.Font()
        self.font.LoadFont(path)
        log.info("Scroller loaded font %s (height=%d)", path, self.font.height)

        self.text = ""
        # Start off the right edge.
        self.top_x = display.width
        self.bottom_x = display.width

        # graphics.DrawText places the text baseline at y. Convert a desired
        # vertical center to a baseline for this font.
        half = self.font.height // 2

        def baseline_for(center):
            return center + self.font.baseline - half

        # A 64x64 stack shows two lines, one centered in each 64x32 panel
        # (centers at 16 and 48). A single short panel (<= 32 tall) can't fit
        # two stacked lines, so draw one line centered on the whole display.
        self.single_line = display.height <= 32
        if self.single_line:
            self.top_y = baseline_for(display.height // 2)
            self.bottom_y = self.top_y  # unused
        else:
            self.top_y = baseline_for(16)
            self.bottom_y = baseline_for(48)

    def set_brightness(self, b):
        self._brightness = b

    def _color_obj(self):
        c = self._color
        b = self._brightness
        return graphics.Color(
            int(((c >> 16) & 0xFF) * b),
            int(((c >> 8) & 0xFF) * b),
            int((c & 0xFF) * b),
        )

    def set_text(self, text):
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        text = str(text)
        log.debug("New scroll text: %r", text)
        self.text = text
        self.text_width = sum(self.font.CharacterWidth(ord(ch)) for ch in text)
        self.top_x = self.display.width
        self.bottom_x = self.display.width
        now = time.monotonic()
        self.start_time = now
        self.last_frame = now

    def tick(self):
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
        width = self.display.width

        new_top = self.top_x - pixels
        if new_top < end_x:
            new_top = width
        self.top_x = new_top

        if not self.single_line and now - self.start_time >= self.offset_seconds:
            new_bot = self.bottom_x - pixels
            if new_bot < end_x:
                new_bot = width
            self.bottom_x = new_bot

    def render(self, canvas):
        if not self.text:
            return
        color = self._color_obj()
        graphics.DrawText(canvas, self.font, self.top_x, self.top_y, color, self.text)
        if not self.single_line:
            graphics.DrawText(canvas, self.font, self.bottom_x, self.bottom_y, color, self.text)