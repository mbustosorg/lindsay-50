"""PNG slideshow pattern.

Displays PNG images from a directory (default: <repo>/design/pngs) on the LED
matrix, advancing to the next image every PNG_INTERVAL seconds while active.

Unlike the rgbmatrix `SetImage` sample, this fits the Effect model so it slots
into the EffectCoordinator rotation and gets the same brightness fade as the
other patterns: each image is downscaled to the panel, quantized to <=256
colors, and loaded into the indexed Bitmap/Palette the renderer expects.
"""

import logging
import re
import time
from pathlib import Path

from rgb_display import Bitmap, Palette, Effect
from lib_shared.config_reader import get_config

logger = logging.getLogger("heart")


def _natural_key(path):
    """Sort key so 'Artboard 2' precedes 'Artboard 10'."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", path.name)]


class PngDisplay(Effect):
    """Slideshow of PNGs, rendered through the indexed Bitmap/Palette pipeline."""

    def __init__(self, display, png_dir=None, interval=8.0, fade=0.6, gamma=2.2):
        cfg = get_config()
        # Match the sibling effects: source geometry from the mapped canvas.
        self._w = display.canvas.width
        self._h = display.canvas.height
        self._interval = float(cfg.if_exists("PNG_INTERVAL") or interval)
        self._fade = float(cfg.if_exists("PNG_FADE") or fade)  # crossfade seconds
        self._gamma = gamma  # perceptually-linear fade, matches EffectCoordinator

        if png_dir is None:
            png_dir = cfg.if_exists("PNG_DIR")
        if png_dir is None:
            # patterns/ -> heart-matrix-controller/ -> repo root -> design/pngs
            png_dir = Path(__file__).resolve().parent.parent.parent / "design" / "pngs"
        self._dir = Path(png_dir)
        self._paths = sorted(self._dir.glob("*.png"), key=_natural_key)

        self._index = 0
        self._coord_b = 1.0   # brightness from the EffectCoordinator's global fades
        self._img_b = 1.0     # internal per-image crossfade level (0..1)
        self._phase = "hold"  # hold -> out -> (swap) -> in -> hold
        self._phase_start = time.monotonic()

        if self._paths:
            logger.info("PngDisplay: %d image(s) from %s", len(self._paths), self._dir)
            self._load_current()
        else:
            logger.warning("PngDisplay: no .png files in %s", self._dir)
            self.bitmap = Bitmap(self._w, self._h)  # all index 0
            self.palette = Palette(1)               # index 0 -> black
            self._init_render()

    # -- image loading ------------------------------------------------------

    def _load_current(self):
        self._render_image(self._paths[self._index])
        self._init_render()    # recapture palette for brightness scaling
        self._apply()          # reapply the current combined fade level

    def _render_image(self, path):
        """Load, fit, and render one PNG as white-on-black into bitmap/palette.

        The source art is a black drawing on a transparent background. Use the
        alpha channel as the ink mask: paint white where the drawing is and
        leave the transparent background black, so it reads on the unlit panel.
        """
        from PIL import Image  # lazy: only the Pi needs Pillow

        w, h = self._w, self._h
        img = Image.open(path).convert("RGBA")
        img.thumbnail((w, h), Image.LANCZOS)  # fit within the panel, keep aspect
        mask = img.getchannel("A")            # drawing = opaque, background = transparent

        frame = Image.new("RGB", (w, h), (0, 0, 0))
        white = Image.new("RGB", img.size, (255, 255, 255))
        offset = ((w - img.width) // 2, (h - img.height) // 2)
        frame.paste(white, offset, mask)      # white where the drawing is, black elsewhere

        quant = frame.quantize(colors=256)
        pal = quant.getpalette() or []
        palette = Palette(256)
        for i in range(len(pal) // 3):
            r, g, b = pal[i * 3 : i * 3 + 3]
            palette[i] = (r << 16) | (g << 8) | b

        bitmap = Bitmap(w, h)
        bitmap._buf[:] = quant.tobytes()  # one palette index per pixel, row-major
        self.bitmap = bitmap
        self.palette = palette

    # -- Effect interface ---------------------------------------------------

    def set_brightness(self, b):
        # Called by the EffectCoordinator for global pattern-switch fades.
        self._coord_b = b
        if b == 0.0:
            # We're (de)activating — restart the slideshow from a full image so
            # the coordinator's fade-in shows a complete frame, not a mid-crossfade.
            self._img_b = 1.0
            self._phase = "hold"
            self._phase_start = time.monotonic()
        self._apply()

    def _apply(self):
        """Drive the palette from the coordinator fade * the per-image fade."""
        super().set_brightness(self._coord_b * self._img_b)

    def tick(self):
        # Slideshow with a crossfade: hold the image, fade it out, swap, fade in.
        if len(self._paths) <= 1:
            return
        elapsed = time.monotonic() - self._phase_start

        if self._phase == "hold":
            if elapsed >= self._interval:
                self._phase = "out"
                self._phase_start = time.monotonic()
            return

        t = elapsed / self._fade if self._fade > 0 else 1.0
        if self._phase == "out":
            if t >= 1.0:
                self._index = (self._index + 1) % len(self._paths)
                self._img_b = 0.0
                self._load_current()          # new image, still dark
                self._phase = "in"
                self._phase_start = time.monotonic()
                return
            self._img_b = (1.0 - t) ** self._gamma
        else:  # "in"
            if t >= 1.0:
                self._img_b = 1.0
                self._phase = "hold"
                self._phase_start = time.monotonic()
            else:
                self._img_b = t ** self._gamma
        self._apply()

    def render(self, canvas):
        """Draw every pixel (a photo fills the panel — no transparent index 0)."""
        colors = self.palette._colors
        buf = self.bitmap._buf
        w, h = self.bitmap.width, self.bitmap.height
        for y in range(h):
            row = y * w
            for x in range(w):
                c = colors[buf[row + x]]
                canvas.SetPixel(x, y, (c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF)