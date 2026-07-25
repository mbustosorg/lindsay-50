"""Immediate-mode rendering substrate for the Raspberry Pi (hzeller rpi-rgb-led-matrix).

Replaces CircuitPython's displayio retained scene graph. The effects keep their
animation logic — they write palette indices into a `Bitmap` and define a
`Palette` — but instead of displayio compositing a scene graph automatically,
the `MatrixDisplay` blits the active effect's bitmap onto an offscreen canvas
each frame and pushes it to the panel with `SwapOnVSync`.

`Bitmap`, `Palette`, and `arrayblit` (the small subset of the displayio /
bitmaptools API the effects use) live in `lib_shared.effect_base`. This module
is only the rgbmatrix-backed display: it owns the matrix hardware and the
double-buffered canvas.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from lib_shared.config_reader import get_config
from lib_shared.display_base import DisplayBase

if TYPE_CHECKING:
    # The C extension builds only on the Pi. The stub at typings/rgbmatrix/
    # gives pyright/pylance a typed view; the lazy import inside __init__
    # below keeps the runtime import contained so importing this module on
    # macOS doesn't crash before MatrixDisplay() is ever instantiated.
    from rgbmatrix import Canvas, RGBMatrix

logger = logging.getLogger("heart")


class MatrixDisplay(DisplayBase):
    """Owns the RGBMatrix and double-buffered canvas.

    Geometry defaults assume a 64x64 logical panel built from two 64x32 HUB75
    panels, serpentine-wired (chain of 2 folded by the U-mapper). All options
    are overridable via settings.toml / env. Verify hardware_mapping and the
    pixel mapper against your actual wiring.
    """

    # Class-level type annotations let pyright/pylance see the attribute types
    # despite the runtime import being lazy (inside __init__, since the C
    # extension only builds on the Pi). The TYPE_CHECKING block at the top of
    # the file provides the names; these annotations consume them.
    _matrix: RGBMatrix
    canvas: Canvas

    def __init__(self):
        from rgbmatrix import RGBMatrix, RGBMatrixOptions  # runtime import

        cfg = get_config()

        def _opt(key, default):
            val = cfg.if_exists(key)
            return val if val is not None else default

        options = RGBMatrixOptions()

        # options.rows = int(_opt("MATRIX_ROWS", 64))
        # options.cols = int(_opt("MATRIX_COLS", 64))
        # options.chain_length = int(_opt("MATRIX_CHAIN", 2))
        # options.parallel = int(_opt("MATRIX_PARALLEL", 1))
        # options.hardware_mapping = _opt("MATRIX_HARDWARE_MAPPING", "regular")
        # options.pixel_mapper_config = _opt("MATRIX_PIXEL_MAPPER", "V-mapper")
        # options.led_multiplexing = _opt("MATRIX_LED_MULTIPLEXING", 1 )
        options.rows = int(_opt("MATRIX_ROWS", 32))
        options.cols = int(_opt("MATRIX_COLS", 64))
        options.chain_length = int(_opt("MATRIX_CHAIN", 2))
        options.parallel = int(_opt("MATRIX_PARALLEL", 1))
        # options.hardware_mapping = _opt("MATRIX_HARDWARE_MAPPING", "regular")
        options.pixel_mapper_config = _opt("MATRIX_PIXEL_MAPPER", "V-mapper;Rotate:180")

        options.pwm_bits = int(_opt("MATRIX_PWM_BITS", 10))
        options.brightness = int(_opt("MATRIX_BRIGHTNESS", 100))
        options.gpio_slowdown = int(_opt("MATRIX_GPIO_SLOWDOWN", 4))
        # Keep root after init (don't drop to 'nobody'); harmless here and avoids
        # surprises if other parts of the process need privileges.
        options.drop_privileges = False
        options.disable_hardware_pulsing = True

        self._matrix = RGBMatrix(options=options)
        self.canvas = self._matrix.CreateFrameCanvas()
        # Decode the hzeller library's bytes-typed pixel_mapper_config to
        # a str before comparing. The library stores the value as
        # `b'U-mapper'` (a bytes literal), and the prior str-vs-bytes
        # comparison always returned False in Python 3, sending every
        # boot down the else-branch (128x32) regardless of config —
        # the root cause of "text not on the panel" with a 64x64 stack.
        mapper = options.pixel_mapper_config
        if isinstance(mapper, bytes):
            mapper = mapper.decode()
        is_u_mapper = mapper == "U-mapper"
        self.width = options.cols * options.chain_length // 2 if is_u_mapper else options.cols * options.chain_length
        self.height = options.rows * 2 if is_u_mapper else options.rows * options.parallel
        # Reused offscreen RGB buffer for the text-scrim compositor path
        # (allocated lazily on first scrim frame; None while the scrim is
        # off so the common no-scrim path allocates nothing).
        self._scrim_buf = None
        logger.info("MatrixDisplay initialized: %dx%d", self.width, self.height)

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

        When the scroller reports non-empty scrim rects (TextSettings.
        text_scrim > 0), the effect is drawn into an offscreen buffer, the
        rectangles behind the text are dimmed by `(1 - scrim)`, and the
        result is blitted — so the pattern shows through, just darker where
        the text sits (the rects hug the text, not the whole width). With
        the scrim off (the default) this is the original direct path: no
        buffer, no numpy/PIL import, no extra blit.
        """
        canvas = self.canvas
        canvas.Clear()
        rects = scroller.scrim_rects() if hasattr(scroller, "scrim_rects") else []
        if rects:
            self._render_with_scrim(canvas, effect, scroller, rects)
        else:
            effect.render(canvas)
        scroller.render(canvas)
        self.canvas = self._matrix.SwapOnVSync(canvas)

    def _render_with_scrim(self, canvas, effect, scroller, rects):
        """Render `effect` into an offscreen buffer, dim the text rects, blit.

        Buffer geometry comes from the CANVAS (`canvas.width` /
        `canvas.height`) — the logical size the pixel mapper exposes and
        the size every effect/scroller renders against — NOT `self.width`
        / `self.height`, which are mis-derived for non-U-mapper configs
        (e.g. the V-mapper 64x64 stack reports self=128x32). Sizing the
        buffer to self.* dropped the effect's lower half and blacked out
        everything below the text.

        numpy + Pillow are imported lazily here (both ship on the Pi via
        requirements-pi; neither is needed on the host, and this path only
        runs when the scrim is enabled)."""
        import numpy as np
        from PIL import Image

        w = int(canvas.width)
        h = int(canvas.height)
        buf = self._scrim_buf
        if buf is None or buf.shape[0] != h or buf.shape[1] != w:
            buf = np.zeros((h, w, 3), dtype=np.uint8)
            self._scrim_buf = buf
        buf.fill(0)
        effect.render(_BufferCanvas(buf))
        factor = 1.0 - float(getattr(scroller, "scrim", 0.0))
        for x0, y0, x1, y1 in rects:
            x0 = max(0, int(x0))
            y0 = max(0, int(y0))
            x1 = min(w, int(x1))
            y1 = min(h, int(y1))
            if x1 > x0 and y1 > y0:
                region = buf[y0:y1, x0:x1]
                dimmed = (region.astype(np.float32) * factor).astype(np.uint8)
                # Round the corners: leave the four corner pixels undimmed so
                # the scrim reads as slightly rounded, not a hard box. Only
                # when the rect is big enough that dropping a corner reads as
                # a bevel rather than eating a sliver.
                rh = y1 - y0
                rw = x1 - x0
                if rw >= 3 and rh >= 3:
                    for cy, cx in ((0, 0), (0, rw - 1), (rh - 1, 0), (rh - 1, rw - 1)):
                        dimmed[cy, cx] = region[cy, cx]
                buf[y0:y1, x0:x1] = dimmed
        canvas.SetImage(Image.fromarray(buf))


class _BufferCanvas:
    """Write-only canvas shim recording draws into a numpy RGB buffer.

    Mirrors the subset of the rgbmatrix Canvas API the effects call
    (`SetPixel` / `SetImage` for palette and full-color effects, plus
    `Clear` / `Fill` and `width` / `height`), so the scrim compositor can
    capture what an effect drew and dim selected rows before blitting the
    whole frame to the real FrameCanvas in one `SetImage`.
    """

    def __init__(self, buf):
        self._buf = buf
        self.height = int(buf.shape[0])
        self.width = int(buf.shape[1])

    def SetPixel(self, x, y, r, g, b):
        if 0 <= x < self.width and 0 <= y < self.height:
            # One numpy write per pixel (assign the whole RGB triple) —
            # ~3x fewer indexing ops than per-channel assignment in the
            # effect's per-pixel loop.
            self._buf[y, x] = (int(r) & 0xFF, int(g) & 0xFF, int(b) & 0xFF)

    def SetImage(self, image, offset_x=0, offset_y=0, *args, **kwargs):
        import numpy as np

        arr = np.asarray(image.convert("RGB") if hasattr(image, "convert") else image)
        h = min(arr.shape[0], self.height - offset_y)
        w = min(arr.shape[1], self.width - offset_x)
        if h > 0 and w > 0:
            self._buf[offset_y : offset_y + h, offset_x : offset_x + w] = arr[:h, :w, :3]

    def Clear(self):
        self._buf.fill(0)

    def Fill(self, r, g, b):
        self._buf[:, :, 0] = int(r) & 0xFF
        self._buf[:, :, 1] = int(g) & 0xFF
        self._buf[:, :, 2] = int(b) & 0xFF
