"""Scrolling text, rendered with the hzeller rpi-rgb-led-matrix graphics API.

Replaces the displayio Label + terminalio.FONT approach. Two copies of the
message scroll right-to-left, one centered in each 64x32 panel, the lower one
lagging by `offset_seconds`. The font is a BDF loaded from FONT_PATH (copy one
from the rpi-rgb-led-matrix `fonts/` directory).

The time/pixel math (text width, x positions, frame pacing, two-line offset)
lives in `lib_shared.scroller_base.ScrollerBase`. This module is the
`rgbmatrix`-specific subclass — it loads a BDF font and calls
`graphics.DrawText` to blit glyphs.
"""

import logging
from rgbmatrix import graphics

from lib_shared.config_reader import get_config
from lib_shared.scroller_base import ScrollerBase

log = logging.getLogger("heart")


class MatrixScroller(ScrollerBase):
    def __init__(
        self,
        display,
        color=0xFF0000,
        frame_delay=0.04,
        offset_seconds=1.0,
        font_path=None,
    ):
        super().__init__(
            frame_delay=frame_delay, offset_seconds=offset_seconds, color=color
        )
        self.display = display

        cfg = get_config()
        path = font_path or cfg.if_exists("FONT_PATH") or "fonts/8x13B.bdf"
        self.font = graphics.Font()
        self.font.LoadFont(path)
        log.info("Scroller loaded font %s (height=%d)", path, self.font.height)

        # The font metrics drive compute_layout — the base class's render
        # uses self.top_y / self.bottom_y, set by compute_layout below.
        self.font_height = self.font.height
        self.font_baseline = self.font.baseline
        self.compute_layout(display.canvas.width, display.canvas.height)

    def compute_layout(self, canvas_width, canvas_height):
        """Place baselines for the top/bottom 64x32 panels (or single short line)."""
        half = self.font_height // 2

        def baseline_for(center):
            return center + self.font_baseline - half

        # A 64x64 stack shows two lines, one centered in each 64x32 panel
        # (centers at 16 and 48). A single short panel (<= 32 tall) can't fit
        # two stacked lines, so draw one line centered on the whole display.
        self.single_line = canvas_height <= 32
        if self.single_line:
            self.top_y = baseline_for(canvas_height // 2)
            self.bottom_y = self.top_y  # unused
        else:
            self.top_y = baseline_for(16)
            self.bottom_y = baseline_for(48)

    def measure_text(self, text):
        return sum(self.font.CharacterWidth(ord(ch)) for ch in text)

    def draw_text(self, canvas, text, x, y, color):
        graphics.DrawText(canvas, self.font, x, y, color, text)

    def _color_obj(self):
        """Return an rgbmatrix.graphics.Color for the current color+brightness."""
        c = self._color
        b = self._brightness
        return graphics.Color(
            int(((c >> 16) & 0xFF) * b),
            int(((c >> 8) & 0xFF) * b),
            int((c & 0xFF) * b),
        )

    def render(self, canvas):
        """Override the base render to use the rgbmatrix graphics.Color object."""
        if not self.text:
            return
        color = self._color_obj()
        self.draw_text(canvas, self.text, self.top_x, self.top_y, color)
        if not self.single_line:
            self.draw_text(canvas, self.text, self.bottom_x, self.bottom_y, color)
