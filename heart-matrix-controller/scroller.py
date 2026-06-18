"""Scrolling text, rendered with the hzeller rpi-rgb-led-matrix graphics API.

Replaces the displayio Label + terminalio.FONT approach. One line of text
scrolls right-to-left, centered vertically on the full display, in orange. The
font is a BDF loaded from FONT_PATH (copy one from the rpi-rgb-led-matrix
`fonts/` directory).

The time/pixel math (text width, x positions, frame pacing) lives in
`lib_shared.scroller_base.ScrollerBase`. This module is the `rgbmatrix`-specific
subclass — it loads a BDF font and calls `graphics.DrawText` to blit glyphs.

v2 config: the user-facing knobs on `TextSettings` are `color`, `speed`, and
`text_effect`. Callers destructure `TextSettings` and pass `color=` / `speed=`
to the scroller. The device's main loop applies live updates via
`scroller.set_color()` and `scroller.set_speed()`.
"""

from __future__ import annotations

import logging
from rgbmatrix import graphics  # noqa: F401 — stub for IDE/pyright, real module on the Pi

from lib_shared.config_reader import get_config
from lib_shared.scroller_base import ScrollerBase

log = logging.getLogger("heart")


class MatrixScroller(ScrollerBase):
    def __init__(
        self,
        display,
        *,
        speed: int = ScrollerBase.DEFAULT_SPEED,
        color: int = 0xFF6400,
        font_path: str | None = None,
    ):
        super().__init__(
            speed=speed,
            color=color,
        )
        self.display = display

        cfg = get_config()
        path = font_path or cfg.if_exists("FONT_PATH") or "fonts/8x13.bdf"
        self.font = graphics.Font()
        self.font.LoadFont(path)
        log.info("Scroller loaded font %s (height=%d)", path, self.font.height)

        # The font metrics drive compute_layout — the base class's render
        # uses self.top_y / self.bottom_y, set by compute_layout below.
        self.font_height = self.font.height
        self.font_baseline = self.font.baseline
        self.compute_layout(display.canvas.width, display.canvas.height)

    def compute_layout(self, canvas_width, canvas_height):
        """Place the baseline for a single line centered on the full display."""
        half = self.font_height // 2

        def baseline_for(center):
            return center + self.font_baseline - half

        # Always one line, centered vertically on the whole display.
        self.single_line = True
        self.top_y = baseline_for(canvas_height // 2)
        self.bottom_y = self.top_y  # unused

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
