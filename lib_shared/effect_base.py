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

    def __init__(self, width, height):
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

    def __init__(self, size):
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
    flame/nightsky effects rely on.
    """
    if len(buf) != len(bitmap._buf):
        raise ValueError("arrayblit expects a full-frame buffer")
    bitmap._buf[:] = buf


class Effect:
    """Base for background effects: palette-based brightness fade + canvas blit.

    Subclasses set `self.bitmap`, `self.palette`, and (optionally) `self.scale`,
    then call `self._init_render()` once the palette is populated.
    """

    bitmap: Bitmap  # subclasses must set
    palette: Palette  # subclasses must set
    scale = 1

    def _init_render(self):
        # Captured for palette-based brightness fading (see set_brightness).
        self._original_palette = [self.palette[i] for i in range(len(self.palette))]

    def set_brightness(self, b):
        for i, c in enumerate(self._original_palette):
            r = int(((c >> 16) & 0xFF) * b)
            g = int(((c >> 8) & 0xFF) * b)
            bl = int((c & 0xFF) * b)
            self.palette[i] = (r << 16) | (g << 8) | bl

    def render(self, canvas):
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
