"""Metaballs — a port of a Pixelblaze 2D pattern (ZRanger1, MIT).

Blobs of fire that drift around the panel, merging and splitting as they
pass. A handful of control points bounce inside a box; each pixel colours
itself from a *multiplicative* voronoi distance field — the running minimum
distance is scaled by each successive point, which is what makes nearby
blobs fuse into one rather than just overlapping. Pixels close to a point
(small field value) glow in the fire band; everything else is black.

Like honeycomb / windfire / CME / marble this paints per-pixel HSV, so it
computes a whole RGB frame and blits it with canvas.SetImage(), vectorized
with numpy.

Pixelblaze primitives, mapped:
    time(0.0008)          the original steps the sim once per wrap of this
                          sawtooth (~52 ms); we advance at that fixed rate
                          off the real frame clock, so motion is FPS-independent
    prng / prngSeed       seeded RNG for the initial point positions/velocities
    hypot                 euclidean distance from pixel to a control point
    wave(v)               (1 + sin(v*2pi)) / 2, shaping the blob brightness
    hsv(h,s,v)            HSV->RGB, hue wraps mod 1

The distance loop is faithful to the original: `minDistance` starts at 1 and
each point does `r = minDistance * hypot(point - pixel) * splatter; minDistance
= min(r, minDistance)` — order-dependent and multiplicative (not a plain
nearest-point voronoi). `numPoints`, `speed`, `splatter`, `edge` are the
original's UI sliders, exposed here as constructor kwargs. The multi-panel
`nodeId()` branch (a dim red glow on other panels) is dropped — a single
logical panel only ever hits the metaballs branch.
"""

import logging
import time

import numpy as np

from lib_shared.effect_base import Effect

# Reuse the shaping + colour helpers rather than duplicating them.
from lib_shared.patterns.windfire import _wave, _TIME_UNIT
from lib_shared.patterns.honeycomb import _hsv_to_rgb

logger = logging.getLogger("heart")

_MAX_POINTS = 8


class Metaballs(Effect):
    """Bouncing fire blobs via a multiplicative distance field, blitted via SetImage()."""

    # The blobs bounce inside this half-box (centred coords run -0.5..0.5).
    _BOX = 0.35

    def __init__(
        self,
        display,
        tf=1.0,
        num_points=3,
        speed=0.05,
        splatter=1.75,
        edge=0.082,
        seed=1,
        max_brightness=0.85,
    ):
        self._w = display.canvas.width
        self._h = display.canvas.height
        # tf scales the animation clock: <1 slows it down, >1 speeds it up.
        self._tf = tf
        self._num = max(1, min(int(num_points), _MAX_POINTS))
        self._speed = speed
        self._splatter = splatter
        self._edge = edge
        self._max_brightness = max_brightness
        self._brightness = 1.0

        # The original advances the sim once per wrap of time(0.0008); mirror
        # that fixed step off the real clock so motion doesn't scale with FPS.
        self._step = 0.0008 * _TIME_UNIT
        self._last = time.monotonic()
        self._accum = 0.0

        # Centered, aspect-preserving coordinates (keeps blobs round on a
        # non-square panel). For 64x64 this is [-0.5, 0.5] on each axis —
        # the same space the original's translate(-0.5, -0.5) produces.
        maxd = max(self._w, self._h)
        denom = max(maxd - 1, 1)
        xs = (np.arange(self._w) - (self._w - 1) / 2.0) / denom
        ys = (np.arange(self._h) - (self._h - 1) / 2.0) / denom
        self._px, self._py = np.meshgrid(xs, ys)  # (h, w)

        # Points as [x, y, vx, vy] rows. prngSeed(1) + prng(1)-0.5 positions
        # and -0.5+prng(1) velocities, both in [-0.5, 0.5].
        rng = np.random.RandomState(seed)
        self._points = np.zeros((self._num, 4))
        for i in range(self._num):
            self._points[i, 0] = rng.random_sample() - 0.5
            self._points[i, 1] = rng.random_sample() - 0.5
            self._points[i, 2] = rng.random_sample() - 0.5
            self._points[i, 3] = rng.random_sample() - 0.5

        self._frame = None
        self._compute()

    def _bounce(self):
        """Advance every point by velocity*speed, reflecting off the box.

        Faithful to the original: at most one axis flips per step (the first
        wall crossed wins — the `continue` in the Pixelblaze loop)."""
        box = self._BOX
        speed = self._speed
        p = self._points
        for i in range(self._num):
            p[i, 0] += p[i, 2] * speed
            p[i, 1] += p[i, 3] * speed
            if p[i, 0] < -box:
                p[i, 0] = -box
                p[i, 2] = -p[i, 2]
            elif p[i, 1] < -box:
                p[i, 1] = -box
                p[i, 3] = -p[i, 3]
            elif p[i, 0] > box:
                p[i, 0] = box
                p[i, 2] = -p[i, 2]
            elif p[i, 1] > box:
                p[i, 1] = box
                p[i, 3] = -p[i, 3]

    def _compute(self):
        px = self._px
        py = self._py
        splat = self._splatter
        p = self._points

        # Multiplicative voronoi field: running min, scaled by each point.
        min_d = np.ones_like(px)
        for i in range(self._num):
            r = min_d * np.hypot(p[i, 0] - px, p[i, 1] - py) * splat
            min_d = np.minimum(r, min_d)

        edge = self._edge
        inside = min_d < edge
        # hsv(edge - minDistance, 1, 1.2 - wave(5*minDistance)): tiny hue span
        # near 0 (deep red -> orange), brightness shaped by the blob interior.
        hue = np.clip(edge - min_d, 0.0, 1.0)
        val = np.clip(1.2 - _wave(5.0 * min_d), 0.0, 1.0)
        rgb = _hsv_to_rgb(hue, 1.0, val) * inside[..., None]
        rgb = rgb * self._max_brightness
        self._frame = np.clip(rgb * 255.0, 0, 255).astype(np.uint8)  # (h, w, 3)

    # -- Effect interface (overridden; no Bitmap/Palette needed) --

    def set_brightness(self, b):
        self._brightness = b

    def tick(self):
        now = time.monotonic()
        dt = (now - self._last) * self._tf
        self._last = now
        # Step the simulation at the fixed rate, catching up after a stall
        # (capped so a long pause can't spiral into thousands of steps).
        self._accum += dt
        steps = 0
        while self._accum >= self._step and steps < 100:
            self._bounce()
            self._accum -= self._step
            steps += 1
        # Hitting the work cap means a real stall dumped a large backlog into
        # _accum. The cap only bounds work per frame, not the accumulator, so a
        # leftover backlog would drain at 100 steps/frame across the following
        # frames — the blobs visibly fast-forward for a while, then settle. Drop
        # the remainder so a long pause costs at most this one burst frame.
        if steps == 100:
            self._accum = 0.0
        self._compute()

    def render(self, canvas):
        from PIL import Image

        arr = self._frame
        if arr is None:
            return
        if self._brightness < 1.0:
            arr = (arr * self._brightness).astype(np.uint8)
        canvas.SetImage(Image.fromarray(arr))
