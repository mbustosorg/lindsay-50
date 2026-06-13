"""Beating red heart — the startup splash.

A filled heart (implicit curve `(x^2+y^2-1)^3 - x^2 y^3 <= 0`) rendered in red,
pulsing with a "lub-dub" envelope so it reads as a heartbeat: it briefly swells
and brightens on each beat, then relaxes.  Palette-based like the other
generative effects, so `Effect` supplies the brightness fade and canvas blit.

This isn't part of the normal rotation — `EffectCoordinator` shows it once at
boot for a few seconds, then fades into the first background effect.
"""

import math
import time

from rgb_display import Bitmap, Palette, Effect, arrayblit

_PALETTE_SIZE = 16

_BPM = 66.0            # beats per minute
_LUB_AT = 0.0          # phase (0..1) of the first, stronger thump
_DUB_AT = 0.18         # phase of the second, softer thump
_THUMP_WIDTH = 0.05    # sharpness of each thump (smaller = crisper)
_DUB_GAIN = 0.6        # the "dub" is softer than the "lub"

_SIZE_BASE = 0.82      # heart scale between beats
_SIZE_SWELL = 0.22     # extra scale at a beat's peak


def _thump(phase, center, width):
    """Gaussian pulse at `center`, measured around the circle (wraps at 1.0)."""
    d = abs(phase - center)
    d = min(d, 1.0 - d)
    return math.exp(-(d / width) ** 2)


def _heart_inside(x, y):
    """True if (x, y) is inside the unit heart `(x^2+y^2-1)^3 - x^2 y^3 <= 0`."""
    a = x * x + y * y - 1.0
    return a * a * a - x * x * y * y * y <= 0.0


class Heartbeat(Effect):
    def __init__(self, display, frame_delay=0.03):
        self.display = display
        self.frame_delay = frame_delay
        self.last_frame = 0.0

        self.w = display.canvas.width
        self.h = display.canvas.height

        # Radius sized so the heart fills the panel at its largest swell with a
        # little margin (0.272 = 0.34 shrunk 20%).
        self.R = min(self.w, self.h) * 0.272
        self.hcx = (self.w - 1) / 2.0
        self.hcy = (self.h - 1) / 2.0

        # The curve isn't symmetric about its own origin (lobes rise above, the
        # point dips below), so find its vertical center once and evaluate the
        # curve shifted by it. Because the pulse scales about that center, the
        # heart stays panel-centered at every beat size — not just one.
        self._y_c = self._curve_center_y()

        self.bitmap = Bitmap(self.w, self.h)
        self.palette = Palette(_PALETTE_SIZE)
        self.palette[0] = 0x000000
        for i in range(1, _PALETTE_SIZE):
            t = i / (_PALETTE_SIZE - 1)
            r = int(70 + 185 * t)
            g = int(25 * t * t)
            b = int(18 * t * t)
            self.palette[i] = (r << 16) | (g << 8) | b
        self._init_render()

        # Precompute each pixel's heart-space coordinates (y flipped so "up" is
        # positive); per frame we only divide by the current pulse size.
        self._nx = [0.0] * (self.w * self.h)
        self._ny = [0.0] * (self.w * self.h)
        for y in range(self.h):
            row = y * self.w
            ny = (self.hcy - y) / self.R
            for x in range(self.w):
                self._nx[row + x] = (x - self.hcx) / self.R
                self._ny[row + x] = ny

        self._buf = bytearray(self.w * self.h)
        self._zero_buf = bytes(self.w * self.h)
        self._rate = _BPM / 60.0

    def _curve_center_y(self):
        """Vertical midpoint of the heart curve in its own (unit) coordinates."""
        steps = 200
        ymin = ymax = None
        for iy in range(steps + 1):
            y = -1.6 + 3.0 * iy / steps
            for ix in range(steps + 1):
                x = -1.4 + 2.8 * ix / steps
                if _heart_inside(x, y):
                    if ymin is None:
                        ymin = y
                    ymax = y
                    break
        return (ymin + ymax) / 2.0 if ymin is not None else 0.0

    def tick(self):
        now = time.monotonic()
        if now - self.last_frame < self.frame_delay:
            return
        self.last_frame = now

        phase = (now * self._rate) % 1.0
        beat = _thump(phase, _LUB_AT, _THUMP_WIDTH) + _DUB_GAIN * _thump(
            phase, _DUB_AT, _THUMP_WIDTH
        )
        if beat > 1.0:
            beat = 1.0

        size = _SIZE_BASE + _SIZE_SWELL * beat
        inv = 1.0 / size
        # Calm heart sits at ~half intensity; a beat pushes it to full red.
        inten = 1 + int((_PALETTE_SIZE - 2) * (0.5 + 0.5 * beat))
        if inten >= _PALETTE_SIZE:
            inten = _PALETTE_SIZE - 1

        nx = self._nx
        ny = self._ny
        y_c = self._y_c
        buf = self._buf
        buf[:] = self._zero_buf
        for i in range(len(buf)):
            hx = nx[i] * inv
            # Shift by the curve's center so the pulse scales about the heart's
            # middle, keeping it panel-centered at every beat size.
            hy = ny[i] * inv + y_c
            a = hx * hx + hy * hy - 1.0
            if a * a * a - hx * hx * hy * hy * hy <= 0.0:
                buf[i] = inten

        arrayblit(self.bitmap, buf)
