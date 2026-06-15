"""Scrolling text, rendered with the hzeller rpi-rgb-led-matrix graphics API.

Replaces the displayio Label + terminalio.FONT approach. One line of text
scrolls right-to-left, centered vertically on the full display, in orange. The
font is a BDF loaded from FONT_PATH (copy one from the rpi-rgb-led-matrix
`fonts/` directory).

The time/pixel math (text width, x positions, frame pacing) lives in
`lib_shared.scroller_base.ScrollerBase`. This module is the `rgbmatrix`-specific
subclass — it loads a BDF font and calls `graphics.DrawText` to blit glyphs.

v2 config: the v1 per-field kwargs (color, frame_delay, offset_seconds) are
still accepted for backwards compatibility, but the recommended entry point
is to pass a `text_settings` (`lib_shared.models.TextSettings`) instance,
which is unpacked into the base class's fields. The device's main loop applies
new text_settings from incoming config envelopes by mutating `self._color`,
`self.frame_delay`, and `self.offset_seconds` in place.
"""

from __future__ import annotations

import logging
from rgbmatrix import graphics  # noqa: F401 — stub for IDE/pyright, real module on the Pi

from lib_shared.config_reader import get_config
from lib_shared.models import TextSettings
from lib_shared.scroller_base import ScrollerBase

log = logging.getLogger("heart")


class MatrixScroller(ScrollerBase):
    def __init__(
        self,
        display,
        color: int = 0xFF6400,
        frame_delay: float = 0.04,
        offset_seconds: float = 1.0,
        font_path: str | None = None,
        text_settings: TextSettings | None = None,
    ):
        if text_settings is not None:
            color = text_settings.color
            frame_delay = text_settings.frame_delay
            offset_seconds = text_settings.offset_seconds
        super().__init__(frame_delay=frame_delay, offset_seconds=offset_seconds, color=color)
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
