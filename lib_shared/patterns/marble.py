"""Marble — a port of a Pixelblaze 3D pattern (tumbling ridged-Perlin volume).

The Pixelblaze original is a `render3D` pattern: it maps the LED coordinates
into a 3D volume of ridged Perlin noise, then in `beforeRender` shrinks that
volume and tumbles it on all three axes so the veined noise appears to rotate
like a glass marble / agate. `perlinRidge` (ridged multifractal) gives the
sharp bright veins; the hue drifts slowly through the cool end of the wheel.

Our panel is a flat 64x64, so there's no 3D pixel map. We treat the panel as a
single plane through the volume (z = 0 at the centre) and apply the same
translate -> scale -> rotateZ -> rotateY -> rotateX transform each frame. The
rotations tilt that plane through the noise field, so we watch an ever-changing
2D slice of the tumbling volume — the same effect the 3D LEDs would show.

Like honeycomb / windfire / CME this paints per-pixel HSV, so it computes a
whole RGB frame and blits it with canvas.SetImage(), vectorized with numpy.

Pixelblaze primitives, mapped:
    time(i)                       sawtooth 0..1, period i*65.536s
    triangle(v) / wave(v)         triangle / raised-sine shaping
    translate3D/scale3D/rotate*   affine transform applied to each coordinate
    perlinRidge(x,y,z,lac,gain,offset,octaves)
                                  ridged multifractal (sharp-veined Perlin)
    smoothstep(e0,e1,v)           Hermite edge ramp, clamped to [0,1]
    hsv(h,s,v)                    HSV->RGB, hue wraps mod 1

The transform is applied to each point in the order the calls are written
(translate first), which is what centres the volume before it rotates. The
original animates the scale (a gentle breathing 0.15..0.30) and the three
rotation angles off independent `time()` clocks; those are reproduced here.
"""

import logging
import time

import numpy as np

from lib_shared.effect_base import Effect

# Reuse the Perlin lattice + gradient primitives and shaping/colour helpers
# rather than duplicating the 256-entry permutation table and HSV math.
from lib_shared.patterns.windfire import _P, _fade, _grad, _lerp, _wave, _triangle, _TIME_UNIT
from lib_shared.patterns.honeycomb import _hsv_to_rgb

logger = logging.getLogger("heart")

PI2 = 2.0 * np.pi


def _smoothstep(edge0, edge1, x):
    t = np.clip((x - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _perlin3(x, y, z):
    """3D improved Perlin noise with x, y AND z as per-pixel arrays.

    windfire's `_perlin` takes a scalar z (a single animation plane); here the
    tumbling plane gives every pixel its own z, so all three axes are arrays.
    Lattice indices wrap every 256 (mask &255), same table as the others.
    """
    x0 = np.floor(x)
    y0 = np.floor(y)
    z0 = np.floor(z)
    xf = x - x0
    yf = y - y0
    zf = z - z0
    xi = x0.astype(np.int32) & 255
    yi = y0.astype(np.int32) & 255
    zi = z0.astype(np.int32) & 255
    u = _fade(xf)
    v = _fade(yf)
    w = _fade(zf)

    P = _P
    A = P[xi] + yi
    AA = P[A] + zi
    AB = P[A + 1] + zi
    B = P[xi + 1] + yi
    BA = P[B] + zi
    BB = P[B + 1] + zi

    x1 = _lerp(u, _grad(P[AA], xf, yf, zf), _grad(P[BA], xf - 1, yf, zf))
    x2 = _lerp(u, _grad(P[AB], xf, yf - 1, zf), _grad(P[BB], xf - 1, yf - 1, zf))
    y1 = _lerp(v, x1, x2)
    x3 = _lerp(u, _grad(P[AA + 1], xf, yf, zf - 1), _grad(P[BA + 1], xf - 1, yf, zf - 1))
    x4 = _lerp(u, _grad(P[AB + 1], xf, yf - 1, zf - 1), _grad(P[BB + 1], xf - 1, yf - 1, zf - 1))
    y2 = _lerp(v, x3, x4)
    return _lerp(w, y1, y2)


def _perlin_ridge(x, y, z, lacunarity, gain, offset, octaves):
    """Ridged multifractal: `(offset - |perlin|)^2` summed over octaves, each
    octave weighted by the previous one. Mirrors Pixelblaze's perlinRidge —
    the squared, offset-folded noise gives the sharp bright veins."""
    total = np.zeros_like(x)
    freq = 1.0
    amp = 0.5
    prev = 1.0
    for _ in range(octaves):
        n = offset - np.abs(_perlin3(x * freq, y * freq, z * freq))
        n = n * n
        total = total + n * amp * prev
        prev = n
        amp *= gain
        freq *= lacunarity
    return total


class Marble(Effect):
    """Tumbling ridged-Perlin volume, sliced per frame, blitted via SetImage()."""

    def __init__(self, display, tf=1.0, max_brightness=0.8):
        self._w = display.canvas.width
        self._h = display.canvas.height
        # tf scales the animation clock: <1 slows it down, >1 speeds it up.
        self._tf = tf
        self._max_brightness = max_brightness
        self._brightness = 1.0
        self._t0 = time.monotonic()

        # Centered, aspect-preserving base coordinates (keeps the marble round
        # on a non-square panel). translate3D(-.5,-.5,-.5) folded in here; the
        # panel is the z = 0 plane through the volume.
        maxd = max(self._w, self._h)
        denom = max(maxd - 1, 1)
        xs = (np.arange(self._w) - (self._w - 1) / 2.0) / denom
        ys = (np.arange(self._h) - (self._h - 1) / 2.0) / denom
        self._X0, self._Y0 = np.meshgrid(xs, ys)  # (h, w), each in ~[-0.5, 0.5]

        self._frame = None
        self._compute()

    def _time(self, interval):
        """Pixelblaze time(interval): sawtooth 0..1 with period interval*65.536s."""
        now = (time.monotonic() - self._t0) * self._tf
        return (now / (interval * _TIME_UNIT)) % 1.0

    def _compute(self):
        t1 = self._time(0.4)

        # scale3D: a gentle breathing zoom, 0.15..0.30.
        s1 = 0.15 + 0.15 * _triangle(self._time(1.0))
        # rotateZ / rotateY / rotateX angles, each on its own clock.
        az = _wave(self._time(0.416)) * PI2
        ay = _wave(self._time(0.515)) * PI2
        ax = _wave(self._time(0.359)) * PI2

        # Apply the transform to every pixel in written order: scale, then
        # rotateZ, rotateY, rotateX. The z axis starts flat (the panel plane),
        # so the rotations are what tilt it into the volume.
        x = self._X0 * s1
        y = self._Y0 * s1
        z = np.zeros_like(x)

        cz, sz = np.cos(az), np.sin(az)
        x, y = x * cz - y * sz, x * sz + y * cz
        cy, sy = np.cos(ay), np.sin(ay)
        x, z = x * cy + z * sy, -x * sy + z * cy
        cx, sx = np.cos(ax), np.sin(ax)
        y, z = y * cx - z * sx, y * sx + z * cx

        p = _perlin_ridge(x, y, z, 2.0, 1.3, 0.75, 5)
        p = p * p

        # hsv(h, s, v*v): hue drifts through the cool end, saturation dips where
        # the veins brighten toward white, value is a smoothstep of the ridge.
        h = _triangle(p + x + y + z) * 0.3 + _triangle(t1) * 0.2 + 0.3
        s = np.clip(1.0 - p * 0.5, 0.0, 1.0)
        v = _smoothstep(0.1, 1.0, p)
        val = np.clip(v * v, 0.0, 1.0)

        rgb = _hsv_to_rgb(h, s, val) * self._max_brightness
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