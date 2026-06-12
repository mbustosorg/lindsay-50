"""Immediate-mode rendering substrate for the Raspberry Pi (hzeller rpi-rgb-led-matrix).

Replaces CircuitPython's displayio retained scene graph. The effects keep their
animation logic — they write palette indices into a `Bitmap` and define a
`Palette` — but instead of displayio compositing a scene graph automatically,
the `Display` blits the active effect's bitmap onto an offscreen canvas each
frame and pushes it to the panel with `SwapOnVSync`.

`Bitmap`, `Palette`, and `arrayblit` mirror the small subset of the displayio /
bitmaptools API the effects use, so their per-pixel animation code is unchanged.
"""

import logging

from lib_shared.config_reader import get_config

logger = logging.getLogger("heart")


class Bitmap:
    """Index buffer mirroring the displayio.Bitmap subset the effects use.

    Stores one palette index per pixel in a flat bytearray (row-major).
    """

    def __init__(self, width, height, _palette_size=0):
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


def arrayblit(bitmap, buf, x=0, y=0, width=None, height=None):
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


class Display:
    """Owns the RGBMatrix and double-buffered canvas.

    Geometry defaults assume a 64x64 logical panel built from two 64x32 HUB75
    panels, serpentine-wired (chain of 2 folded by the U-mapper). All options
    are overridable via settings.toml / env. Verify hardware_mapping and the
    pixel mapper against your actual wiring.
    """

    def __init__(self):
        from rgbmatrix import RGBMatrix, RGBMatrixOptions

        cfg = get_config()

        def _opt(key, default):
            val = cfg.if_exists(key)
            return val if val is not None else default

        options = RGBMatrixOptions()
        options.rows = int(_opt("MATRIX_ROWS", 32))
        options.cols = int(_opt("MATRIX_COLS", 64))
        options.chain_length = int(_opt("MATRIX_CHAIN", 2))
        # options.parallel = int(_opt("MATRIX_PARALLEL", 1))
        # options.hardware_mapping = _opt("MATRIX_HARDWARE_MAPPING", "regular")
        options.pixel_mapper_config = _opt("MATRIX_PIXEL_MAPPER", "U-mapper")
        options.pwm_bits = int(_opt("MATRIX_PWM_BITS", 10))
        options.brightness = int(_opt("MATRIX_BRIGHTNESS", 100))
        options.gpio_slowdown = int(_opt("MATRIX_GPIO_SLOWDOWN", 4))
        # Keep root after init (don't drop to 'nobody'); harmless here and avoids
        # surprises if other parts of the process need privileges.
        options.drop_privileges = False
        options.disable_hardware_pulsing = True

        self._matrix = RGBMatrix(options=options)
        self.canvas = self._matrix.CreateFrameCanvas()
        self.width = (
            options.cols * options.chain_length // 2
            if options.pixel_mapper_config == "U-mapper"
            else options.cols * options.chain_length
        )
        self.height = (
            options.rows * 2
            if options.pixel_mapper_config == "U-mapper"
            else options.rows * options.parallel
        )
        logger.info("Display initialized: %dx%d", self.width, self.height)

    def clear(self):
        """Blank the panel immediately so no frame stays lit after we exit.

        Clears both the live matrix and the offscreen canvas (and swaps it in)
        so the LEDs go dark regardless of which buffer the panel is showing.
        """
        self._matrix.Clear()
        self.canvas.Clear()
        self.canvas = self._matrix.SwapOnVSync(self.canvas)

    def render(self, effect, scroller):
        """Composite one frame: clear, draw the active effect, draw text, swap.

        SwapOnVSync blocks until the panel's next vertical refresh, which paces
        the main loop — no manual sleep needed.
        """
        canvas = self.canvas
        canvas.Clear()
        effect.render(canvas)
        scroller.render(canvas)
        self.canvas = self._matrix.SwapOnVSync(canvas)
