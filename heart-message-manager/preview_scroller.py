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
        MatrixScroller (one centered line). `canvas_width` is intentionally
        unused — the preview scroller is a single centered line, so the
        vertical center only depends on `canvas_height`.
        """
        _ = canvas_width  # see docstring — preview is single-line centered
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

    def draw_text(self, canvas, text, x, y, color):  # pyright: ignore[reportUnusedParameter]
        """Blit `text` at (x, y) on the canvas with brightness tracked in alpha.

        The `color` argument from the base class is the brightness-scaled
        RGB and is deliberately ignored here — we re-derive the un-scaled
        color from `self._color` so the alpha track stays the single
        source of fade truth.

        Two invariants have to hold for the scroller text to read
        correctly on top of the BrowserMediaOverlay's DOM `<img>`/`<video>`:

        1. **Anti-aliased edges must be smooth, not opaque-dim.** If the
           AA coverage is baked into a binary mask (any non-black pixel
           → alpha 255), the edges land as dim-color-opaque pixels and
           read as a "shadow" / "bleed through" halo when the media is
           the background. Instead we capture the AA coverage in the
           alpha channel directly: render text in WHITE on black, take
           the L intensity (which equals the coverage for white-on-black
           since the L-luma formula R*299+G*587+B*114 reduces to v
           when R=G=B=v), and use that as the per-pixel alpha. RGB
           stays at the un-scaled text color, so the interior renders
           at full intensity and the edges fade smoothly to transparent
           — the media behind shows through the edge pixels naturally.

        2. **Brightness must drive ALPHA, not RGB.** The base class's
           `color_tuple()` scales the text color by brightness
           (1.0 → full color, 0.0 → black), and the scroller's fade-out
           during the coordinator's `text_out` phase goes 1.0 → 0.0.
           If that scaled color landed on the canvas as opaque pixels,
           brightness 0 would be opaque black pixels — the "black dots"
           the user saw persisting through the fade. Scaling the alpha
           band by brightness instead means brightness 0 ⇒ alpha 0
           everywhere, and the text vanishes cleanly.
        """
        _ = color  # see docstring — we use self._color for the RGB instead
        target = canvas.image if hasattr(canvas, "image") else canvas
        orig_r = (self._color >> 16) & 0xFF
        orig_g = (self._color >> 8) & 0xFF
        orig_b = self._color & 0xFF
        # Render text in WHITE on black. The L intensity then equals
        # the anti-aliased coverage directly (0 at background, 255 at
        # text interior, smooth at the edges) — independent of the
        # text color, so a darker scroller color doesn't drag the
        # alpha coverage down with it.
        temp = Image.new("RGB", target.size, (0, 0, 0))
        ImageDraw.Draw(temp).text((x, y), text, fill=(255, 255, 255), font=self.font)
        # Scale the coverage by brightness. At b=0 the alpha is 0
        # everywhere, the paste is a no-op, and the text vanishes
        # without leaving opaque black pixels on the canvas.
        alpha_band = temp.convert("L").point(lambda v: int(v * self._brightness))
        # RGBA: un-scaled text color in RGB, brightness-scaled
        # coverage in A.
        opaque = Image.new("RGBA", target.size, (orig_r, orig_g, orig_b, 0))
        opaque.putalpha(alpha_band)
        # Paste with the alpha band as the mask — Pillow multiplies
        # the source alpha by the mask alpha, so the resulting alpha
        # is exactly `alpha_band`.
        target.paste(opaque, (0, 0), mask=alpha_band)

    def render(self, canvas):
        """Override the base render so the canvas is cleared first.

        The base class assumes the EffectCoordinator.clear() handles that,
        but the preview's per-frame loop in preview.js doesn't clear
        between frames — each effect's render() handles its own clear, but
        the scroller blits on top of the existing effect. No clear is
        actually needed for the scroller itself; we delegate to base.
        """
        super().render(canvas)
