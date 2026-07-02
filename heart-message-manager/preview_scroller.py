"""Browser-side scroller for the sign preview.

Pillow-backed subclass of ScrollerBase. Loads a TrueType font and blits text
to a Pillow image (the WebCanvas's underlying buffer). The time/pixel math
is identical to the device's MatrixScroller — same frame_delay, same
offset_seconds, same single orange line centered on the full display — so what
scrolls on the preview matches what the sign will display, frame for frame.

Lives in heart-message-manager/ because it's a preview-specific deliverable
(it depends on Pillow, which the device's Python install may not have).
"""

import logging
import os

from PIL import Image, ImageDraw, ImageFont

from lib_shared.scroller_base import ScrollerBase

log = logging.getLogger("heart")


class PreviewScroller(ScrollerBase):
    """ScrollerBase subclass backed by Pillow. Used in the browser preview.

    The TTF font is loaded from PREVIEW_FONT_PATH (env) or
    `font_path` constructor kwarg; if neither is set, falls back to
    Pillow's bundled default font, which is always available.
    """

    def __init__(
        self, display, *, speed: int = ScrollerBase.DEFAULT_SPEED, color: int = 0xFF6400, font_path: str | None = None
    ):
        super().__init__(speed=speed, color=color)
        self.display = display

        path = font_path or os.environ.get("PREVIEW_FONT_PATH")
        if path:
            self.font = ImageFont.truetype(path, size=11)
            log.info("PreviewScroller loaded font %s", path)
        else:
            # Bundled with Pillow — no external asset required
            self.font = ImageFont.load_default()
            log.info("PreviewScroller using Pillow's default font")
        # font_height / font_baseline drive compute_layout for the
        # top_y / bottom_y positions used by the base class's render.
        ascent, descent = self.font.getmetrics()
        self.font_height = ascent + descent
        self.font_baseline = ascent
        self.compute_layout(display.width, display.height)

    def compute_layout(self, canvas_width, canvas_height):
        """Place the baseline for a single line centered on the full display.

        Pillow's text() draws with the y coordinate as the TOP of the glyph,
        so top_y = vertical_center - (font_height // 2). Matches the device's
        MatrixScroller (one centered line).
        """
        self.single_line = True
        self.top_y = (canvas_height - self.font_height) // 2
        self.bottom_y = self.top_y  # unused

    def measure_text(self, text):
        """Return the pixel width of `text` rendered in self.font.

        Uses getbbox which returns (left, top, right, bottom) — the
        right edge minus the left edge is the ink width.
        """
        bbox = self.font.getbbox(text)
        if bbox is None:
            return 0
        return bbox[2] - bbox[0]

    def draw_text(self, canvas, text, x, y, color):
        """Blit `text` at (x, y) on the canvas (a WebCanvas or Pillow Image).

        The base class passes the WebCanvas instance; the actual Pillow image
        lives at canvas.image. ImageDraw wraps that image and writes the
        glyphs in the requested color (a 3-tuple scaled by brightness).
        """
        target = canvas.image if hasattr(canvas, "image") else canvas
        draw = ImageDraw.Draw(target)
        draw.text((x, y), text, fill=color, font=self.font)

    def render(self, canvas):
        """Override the base render so the canvas is cleared first.

        The base class assumes the EffectCoordinator.clear() handles that,
        but the preview's per-frame loop in preview.js doesn't clear
        between frames — each effect's render() handles its own clear, but
        the scroller blits on top of the existing effect. No clear is
        actually needed for the scroller itself; we delegate to base.
        """
        super().render(canvas)
