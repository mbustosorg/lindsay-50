"""CoronalMassEjection — a port of a Pixelblaze 2D pattern (ZRanger1 / wizard).

A white-hot star at the center throws off Perlin-turbulence "flares" that
churn and occasionally fling super-hot bits outward. The pattern works in
radial coordinates: each pixel's angle drives one noise axis and its radius
the other, so the noise field wraps seamlessly around the star.

Like honeycomb / windfire this doesn't fit the indexed Bitmap/Palette
pipeline (it paints per-pixel HSV), so it computes a whole RGB frame and
blits it with canvas.SetImage(), vectorized with numpy.

Pixelblaze primitives, mapped:
    time(i)                 sawtooth 0..1, period i*65.536s
    hypot / atan2           radial coordinate conversion
    setPerlinWrap(x,y,z)    make Perlin periodic with those integer periods
    perlinTurbulence(...)   sum of |perlin| over octaves (fractal turbulence)
    smoothstep(e0,e1,v)     Hermite edge ramp, clamped to [0,1]
    hsv(h,s,v)              HSV->RGB, hue wraps mod 1

The angular axis is scaled so its full sweep (-pi..pi) spans exactly the
Perlin x-wrap period (`density`), which is what makes the ring of flares
join without a seam. The original exposes density / fractal-gain /
iterations / mirror / cutoff as UI controls; they're constructor kwargs here.
"""

import logging
import time

import numpy as np

from lib_shared.effect_base import Effect

# Reuse the Perlin lattice + gradient primitives and the HSV helper rather
# than duplicating the 256-entry permutation table / colour math.
from lib_shared.patterns.windfire import _P, _fade, _grad, _lerp, _TIME_UNIT
from lib_shared.patterns.honeycomb import _hsv_to_rgb

logger = logging.getLogger("heart")


def _smoothstep(edge0, edge1, x):
    t = np.clip((x - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _perlin_wrapped(x, y, z, wx, wy, wz):
    """3D Perlin noise periodic with integer periods (wx, wy, wz).

    Lattice indices are taken modulo the wrap period before hashing, so the
    +1 neighbour of the last cell hashes to cell 0 — the noise tiles
    seamlessly. x, y are arrays; z a scalar. Mirrors Pixelblaze's
    setPerlinWrap behaviour.
    """
    x0 = np.floor(x)
    y0 = np.floor(y)
    z0 = np.floor(z)
    xf = x - x0
    yf = y - y0
    zf = z - z0
    xi0 = x0.astype(np.int32) % wx
    xi1 = (x0.astype(np.int32) + 1) % wx
    yi0 = y0.astype(np.int32) % wy
    yi1 = (y0.astype(np.int32) + 1) % wy
    zi0 = int(z0) % wz
    zi1 = (int(z0) + 1) % wz
    u = _fade(xf)
    v = _fade(yf)
    w = _fade(zf)

    def h(xi, yi, zi):
        # Every index stays < len(_P) (512): _P[..] <= 255, +yi/+zi <= 510.
        return _P[_P[_P[xi] + yi] + zi]

    x1 = _lerp(
        u,
        _grad(h(xi0, yi0, zi0), xf, yf, zf),
        _grad(h(xi1, yi0, zi0), xf - 1, yf, zf),
    )
    x2 = _lerp(
        u,
        _grad(h(xi0, yi1, zi0), xf, yf - 1, zf),
        _grad(h(xi1, yi1, zi0), xf - 1, yf - 1, zf),
    )
    y1 = _lerp(v, x1, x2)
    x3 = _lerp(
        u,
        _grad(h(xi0, yi0, zi1), xf, yf, zf - 1),
        _grad(h(xi1, yi0, zi1), xf - 1, yf, zf - 1),
    )
    x4 = _lerp(
        u,
        _grad(h(xi0, yi1, zi1), xf, yf - 1, zf - 1),
        _grad(h(xi1, yi1, zi1), xf - 1, yf - 1, zf - 1),
    )
    y2 = _lerp(v, x3, x4)
    return _lerp(w, y1, y2)


def _perlin_turbulence_wrapped(x, y, z, lacunarity, gain, octaves, wx, wy, wz):
    """Fractal turbulence over the wrapped Perlin. Coords scale by frequency;
    the wrap period stays fixed so every octave stays seam-continuous (the
    angular span is an integer multiple of `wx`)."""
    total = np.zeros_like(x)
    freq = 1.0
    amp = 1.0
    for _ in range(octaves):
        total += np.abs(_perlin_wrapped(x * freq, y * freq, z * freq, wx, wy, wz)) * amp
        freq *= lacunarity
        amp *= gain
    return total


class CoronalMassEjection(Effect):
    """Radial Perlin-turbulence star with flares, blitted via canvas.SetImage()."""

    _CORE_SIZE = 0.1

    def __init__(
        self,
        display,
        tf=1.0,
        density=6,
        gain=0.25,
        iterations=3,
        mirror=False,
        cutoff=0.675,
        max_brightness=0.8,
    ):
        self._w = display.canvas.width
        self._h = display.canvas.height
        # tf scales the animation clock: <1 slows it down, >1 speeds it up.
        self._tf = tf
        self._density = int(density)
        self._gain = gain
        self._iterations = int(iterations)
        self._mirror = mirror
        self._cutoff = cutoff
        self._max_brightness = max_brightness
        self._brightness = 1.0
        self._c2 = self._CORE_SIZE / 4.0
        self._t0 = time.monotonic()

        # Centered, aspect-preserving coordinates (keeps the star round on a
        # non-square panel). For a 64x64 panel this is [-0.5, 0.5] on each axis.
        maxd = max(self._w, self._h)
        denom = max(maxd - 1, 1)
        xs = (np.arange(self._w) - (self._w - 1) / 2.0) / denom
        ys = (np.arange(self._h) - (self._h - 1) / 2.0) / denom
        X, Y = np.meshgrid(xs, ys)  # (h, w)
        self._radius = np.hypot(X, Y)
        self._angle = np.arctan2(Y, X)

        self._frame = None
        self._compute()

    def _time(self, interval):
        """Pixelblaze time(interval): sawtooth 0..1 with period interval*65.536s."""
        now = (time.monotonic() - self._t0) * self._tf
        return (now / (interval * _TIME_UNIT)) % 1.0

    def _compute(self):
        t1 = self._time(0.2)
        # perlin z/y wrap at 256, so sweeping across 0..256 loops seamlessly.
        noise_time = self._time(10.0) * 256.0
        noise_y_time = self._time(8.0) * 256.0

        density = self._density
        # The angular axis is scaled so its -pi..pi sweep spans exactly the
        # x-wrap period (`density`) — mirror doubles the sweep. That alignment
        # is what makes the ring of flares join without a seam.
        density_pi_conversion = (1.0 / np.pi) * density * (1.0 if self._mirror else 0.5)

        radius = self._radius
        xn = self._angle * density_pi_conversion

        # Wrap: x-period = density (the angular seam), y/z-period = 256 (time).
        v = 1.0 - _perlin_turbulence_wrapped(
            xn,
            radius - noise_y_time,
            noise_time,
            2.0,
            self._gain,
            self._iterations,
            density,
            256,
            256,
        )

        # Discrete radial flares from the noise, plus an always-white-hot core
        # that blooms past 1 near the center (clamped when painted).
        core = 1.0 - ((radius * v) - self._c2) / self._CORE_SIZE
        v = np.maximum(_smoothstep(self._cutoff, 1.0, v), core)
        v = v * v * v

        # hsv(t1 - 0.125*v, 6.5*radius - v, v): hue drifts with time, saturation
        # rises with radius (white core), value is the flare intensity.
        hue = (t1 - 0.125 * v) % 1.0
        sat = np.clip(6.5 * radius - v, 0.0, 1.0)
        val = np.clip(v, 0.0, 1.0)

        rgb = _hsv_to_rgb(hue, sat, val) * self._max_brightness
        self._frame = np.clip(rgb * 255.0, 0, 255).astype(np.uint8)  # (h, w, 3)

    # -- Effect interface (overridden; no Bitmap/Palette needed) --

    def set_brightness(self, b):
        self._brightness = b

    def tick(self):
        self._compute()

    def render(self, canvas):
        from PIL import Image

        arr = self._frame
        if arr is None:
            return
        if self._brightness < 1.0:
            arr = (arr * self._brightness).astype(np.uint8)
        canvas.SetImage(Image.fromarray(arr))
