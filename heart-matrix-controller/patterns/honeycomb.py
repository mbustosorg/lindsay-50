"""Honeycomb — a port of a Pixelblaze 2D pattern (honeycomb.js).

The Pixelblaze original renders a full-color HSV interference field per pixel.
That doesn't fit the indexed Bitmap/Palette pipeline the other effects use, so
(like the video pattern) this computes a whole RGB frame and blits it with
canvas.SetImage(). The per-pixel math is vectorized with numpy to stay
real-time at 64x64.

Pixelblaze primitives, mapped:
    time(i)     sawtooth 0..1, period i*65.536s  -> here: period passed in seconds
    wave(v)     (1 + sin(v*2pi)) / 2
    triangle(v) 1 - |1 - 2*frac(v)|
    hsv(h,1,v)  HSV->RGB, hue wraps mod 1
"""

import logging
import time

import numpy as np

from rgb_display import Effect

logger = logging.getLogger("heart")

PI2 = 2.0 * np.pi


def _wave(v):
    return (1.0 + np.sin(v * PI2)) * 0.5


def _triangle(v):
    frac = v - np.floor(v)
    return 1.0 - np.abs(1.0 - 2.0 * frac)


def _hsv_to_rgb(h, s, v):
    """Vectorized HSV->RGB. h,v are arrays in 0..1, s scalar; returns (...,3)."""
    h = (h % 1.0) * 6.0
    # np.choose in numpy 2.x (shipped in Pyodide) requires int32 indices;
    # int64 raises "Cannot cast array data from dtype('int64') to
    # dtype('int32')". Cast here so the pattern works in both the
    # browser (Pyodide) and on the Pi.
    i = np.floor(h).astype(np.int32)
    f = h - i
    p = v * (1.0 - s)
    q = v * (1.0 - f * s)
    t = v * (1.0 - (1.0 - f) * s)
    i = i % 6
    r = np.choose(i, [v, q, p, p, t, v])
    g = np.choose(i, [t, v, v, q, p, p])
    b = np.choose(i, [p, p, t, v, v, q])
    return np.stack([r, g, b], axis=-1)


class Honeycomb(Effect):
    """Animated HSV honeycomb interference field, blitted via canvas.SetImage()."""

    def __init__(self, display, tf=15.0):
        self._w = display.canvas.width
        self._h = display.canvas.height
        self._tf = tf  # animation duration; smaller = faster
        self._brightness = 1.0
        self._t0 = time.monotonic()

        # Normalized coordinates on the longest axis (keeps aspect ratio).
        m = float(max(self._w, self._h))
        xs = np.arange(self._w) / m
        ys = np.arange(self._h) / m
        self._X, self._Y = np.meshgrid(xs, ys)  # (h, w)

        self._frame = None
        self._compute()

    def _compute(self):
        tf = self._tf
        now = time.monotonic() - self._t0

        def T(period_s):  # Pixelblaze time() over a period in seconds
            return (now / period_s) % 1.0

        f = _wave(T(tf * 6.6)) * 5.0 + 7.0  # cell density
        t1 = _wave(T(tf * 9.8)) * PI2  # x shift
        t2 = _wave(T(tf * 12.5)) * PI2  # y shift
        t3 = _wave(T(tf * 9.8))  # hue shift
        t4 = T(tf * 0.66)  # value shift

        z = (1.0 + np.sin(self._X * f + t1) + np.cos(self._Y * f + t2)) * 0.5
        v = _wave(z + t4)
        v = v * v * v
        h = _triangle(z) / 2.0 + t3

        rgb = _hsv_to_rgb(h, 1.0, np.clip(v, 0.0, 1.0))
        self._frame = (rgb * 255.0).astype(np.uint8)  # (h, w, 3)

    # -- Effect interface (overridden; no Bitmap/Palette needed) --

    def set_brightness(self, b):
        self._brightness = b

    def tick(self):
        self._compute()

    def render(self, canvas):
        from PIL import Image

        arr = self._frame
        if self._brightness < 1.0:
            arr = (arr * self._brightness).astype(np.uint8)
        canvas.SetImage(Image.fromarray(arr))
