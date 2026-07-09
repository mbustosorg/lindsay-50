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

        #options.rows = int(_opt("MATRIX_ROWS", 64))
        #options.cols = int(_opt("MATRIX_COLS", 64))
        #options.chain_length = int(_opt("MATRIX_CHAIN", 2))
        # options.parallel = int(_opt("MATRIX_PARALLEL", 1))
        #options.hardware_mapping = _opt("MATRIX_HARDWARE_MAPPING", "regular")
        #options.pixel_mapper_config = _opt("MATRIX_PIXEL_MAPPER", "V-mapper")
        #options.led_multiplexing = _opt("MATRIX_LED_MULTIPLEXING", 1 )
        options.rows = int(_opt("MATRIX_ROWS", 32))
        options.cols = int(_opt("MATRIX_COLS", 64))
        options.chain_length = int(_opt("MATRIX_CHAIN", 2))
        options.parallel = int(_opt("MATRIX_PARALLEL", 1))
        #options.hardware_mapping = _opt("MATRIX_HARDWARE_MAPPING", "regular")
        options.pixel_mapper_config = _opt("MATRIX_PIXEL_MAPPER", "V-mapper")

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
        """
        canvas = self.canvas
        canvas.Clear()
        effect.render(canvas)
        scroller.render(canvas)
        self.canvas = self._matrix.SwapOnVSync(canvas)
