"""Rendering primitives shared by the Pi device and the browser preview.

`Bitmap`, `Palette`, `arrayblit`, and the `Effect` base class are the
displayio / bitmaptools subset the patterns use. They have no rgbmatrix
dependency, so they live here — both the Pi (heart-matrix-controller) and
the browser preview (heart-message-manager) import from this module.
The rgbmatrix-backed display class is NOT here; it lives next to the Pi
driver in heart-matrix-controller/rgb_matrix_display.py.
"""


class Bitmap:
    """Index buffer mirroring the displayio.Bitmap subset the effects use.

    Stores one palette index per pixel in a flat bytearray (row-major).
    """

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self._buf = bytearray(width * height)

    def __setitem__(self, xy, value):
        x, y = xy
        self._buf[y * self.width + x] = value

    def __getitem__(self, xy):
        x, y = xy
        return self._buf[y * self.width + x]

    def fill(self, value):
        if value == 0:
            self._buf[:] = bytes(len(self._buf))
        else:
            self._buf[:] = bytes([value]) * len(self._buf)


class Palette:
    """Fixed-size list of 0xRRGGBB colors, indexed like displayio.Palette."""

    def __init__(self, size: int) -> None:
        self._colors = [0] * size

    def __setitem__(self, i, color):
        self._colors[i] = color

    def __getitem__(self, i):
        return self._colors[i]

    def __len__(self):
        return len(self._colors)


def arrayblit(bitmap, buf):
    """Copy a flat index buffer into a Bitmap in one shot (full-frame only).

    Mirrors bitmaptools.arrayblit for the at-origin, whole-bitmap case the
    nightsky effect relies on.
    """
    if len(buf) != len(bitmap._buf):
        raise ValueError("arrayblit expects a full-frame buffer")
    bitmap._buf[:] = buf


class Effect:
    """Base for background effects: palette-based brightness fade + canvas blit.

    Subclasses set `self.bitmap`, `self.palette`, and (optionally) `self.scale`,
    then call `self._init_render()` once the palette is populated. Subclasses
    must implement `tick()` to advance one frame; the default `render()`
    handles the indexed Bitmap → canvas blit but full-color effects (video,
    PNG) override it.
    """

    bitmap: Bitmap  # subclasses must set
    palette: Palette  # subclasses must set
    scale = 1

    def _init_render(self):
        # Captured for palette-based brightness fading (see set_brightness).
        self._original_palette = [self.palette[i] for i in range(len(self.palette))]

    def tick(self) -> None:
        """Advance one frame. Subclasses must override."""
        raise NotImplementedError

    def set_brightness(self, b: float) -> None:
        # Clamp at the 8-bit channel ceiling so callers can pass `b`
        # slightly above 1.0 to push dark pixels brighter (e.g. the
        # MediaCycler's brightness boost) without wrapping saturated
        # channels into corrupted colors. `int()` alone truncates the
        # fractional component; `min(0xFF, …)` keeps the result on
        # the panel's 24-bit RGB color wheel. At b=1.0 the clamp is
        # a no-op for any palette entry already ≤ 0xFF per channel;
        # at b > 1.0 the clamp holds saturated entries at 0xFF and
        # only dark entries get pulled brighter.
        for i, c in enumerate(self._original_palette):
            r = min(0xFF, int(((c >> 16) & 0xFF) * b))
            g = min(0xFF, int(((c >> 8) & 0xFF) * b))
            bl = min(0xFF, int((c & 0xFF) * b))
            self.palette[i] = (r << 16) | (g << 8) | bl

    def render(self, canvas) -> None:
        """Blit nonzero pixels onto the canvas, honoring self.scale.

        Index 0 is the (black) background and is skipped — the canvas is cleared
        before each effect renders, so leaving those pixels untouched is correct.
        """
        bitmap = self.bitmap
        colors = self.palette._colors
        buf = bitmap._buf
        w = bitmap.width
        h = bitmap.height
        s = self.scale
        for y in range(h):
            row = y * w
            for x in range(w):
                idx = buf[row + x]
                if not idx:
                    continue
                c = colors[idx]
                r = (c >> 16) & 0xFF
                g = (c >> 8) & 0xFF
                b = c & 0xFF
                if s == 1:
                    canvas.SetPixel(x, y, r, g, b)
                else:
                    px = x * s
                    py = y * s
                    for dy in range(s):
                        for dx in range(s):
                            canvas.SetPixel(px + dx, py + dy, r, g, b)
