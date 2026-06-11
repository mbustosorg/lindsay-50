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

    def __init__(self, display, png_dir=None, interval=8.0):
        cfg = get_config()
        # Match the sibling effects: source geometry from the mapped canvas.
        self._w = display.canvas.width
        self._h = display.canvas.height
        self._interval = float(cfg.if_exists("PNG_INTERVAL") or interval)

        if png_dir is None:
            png_dir = cfg.if_exists("PNG_DIR")
        if png_dir is None:
            # patterns/ -> heart-matrix-controller/ -> repo root -> design/pngs
            png_dir = Path(__file__).resolve().parent.parent.parent / "design" / "pngs"
        self._dir = Path(png_dir)
        self._paths = sorted(self._dir.glob("*.png"), key=_natural_key)

        self._index = 0
        self._brightness = 1.0
        self._last_advance = time.monotonic()

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
        self._init_render()              # recapture palette for brightness fade
        super().set_brightness(self._brightness)  # reapply current fade level

    def _render_image(self, path):
        """Load, fit, and quantize one PNG into self.bitmap / self.palette."""
        from PIL import Image  # lazy: only the Pi needs Pillow

        w, h = self._w, self._h
        img = Image.open(path)
        img.thumbnail((w, h), Image.LANCZOS)  # fit within the panel, keep aspect
        frame = Image.new("RGB", (w, h), (0, 0, 0))
        offset = ((w - img.width) // 2, (h - img.height) // 2)
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            # Composite over black using the alpha channel as the paste mask.
            img = img.convert("RGBA")
            frame.paste(img, offset, img)
        else:
            frame.paste(img.convert("RGB"), offset)

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
        self._brightness = b
        super().set_brightness(b)

    def tick(self):
        if len(self._paths) <= 1:
            return
        now = time.monotonic()
        if now - self._last_advance >= self._interval:
            self._last_advance = now
            self._index = (self._index + 1) % len(self._paths)
            self._load_current()

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