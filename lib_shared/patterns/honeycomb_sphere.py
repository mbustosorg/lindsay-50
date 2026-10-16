"""HoneycombSphere — the honeycomb field painted on a spinning sphere.

Same HSV interference math as honeycomb.py, but each panel pixel is first
mapped onto the surface of a sphere through a full-panel fisheye: cells near
the center are magnified, and toward the panel edge the surface wraps away
(the edge midpoints sit on the sphere's limb; the corners reach around onto
the back side). The sphere spins on a tilted axis, so cells enter at one
side, swell through the middle and wrap off the other side.

Because the field is evaluated on the 3D surface point (not on lon/lat),
there is no seam and no pole pinching. A third sin term in z keeps cells
from stretching where the surface turns edge-on to the x/y plane.

Like honeycomb, this computes a whole RGB frame with numpy and blits it via
canvas.SetImage().
"""

import logging
import math
import time

import numpy as np

from lib_shared.effect_base import Effect
from lib_shared.patterns.honeycomb import PI2, _hsv_to_rgb, _triangle, _wave

logger = logging.getLogger("heart")


class HoneycombSphere(Effect):
    """Honeycomb field on a spinning fisheye sphere, blitted via canvas.SetImage()."""

    def __init__(
        self,
        display,
        tf=15.0,
        bulge=2.5,
        sphere_radius=0.4,
        spin_period=24.0,
        tilt_deg=20.0,
        edge_dim=0.55,
    ):
        self._w = display.canvas.width
        self._h = display.canvas.height
        self._tf = tf  # animation duration; smaller = faster
        self._sphere_radius = sphere_radius  # field units; smaller = bigger cells
        self._spin_period = spin_period  # seconds per revolution
        self._brightness = 1.0
        self._t0 = time.monotonic()

        # Pixel centers in [-1, 1] on the longest axis (keeps aspect ratio).
        half = max(self._w, self._h) / 2.0
        xs = (np.arange(self._w) + 0.5 - self._w / 2.0) / half
        ys = (np.arange(self._h) + 0.5 - self._h / 2.0) / half
        X, Y = np.meshgrid(xs, ys)  # (h, w)
        r = np.hypot(X, Y)

        # Fisheye: angle from the view axis, theta(r) = c1*r + c2*r^3.
        # The slope at the edge is `bulge` times the slope at the center (so
        # the center is magnified by that much relative to the edge), and the
        # edge midpoint (r=1) lands on the limb (theta = pi/2).
        c1 = (math.pi / 2.0) / (1.0 + (bulge - 1.0) / 3.0)
        c2 = c1 * (bulge - 1.0) / 3.0
        theta = c1 * r + c2 * r**3

        safe_r = np.where(r > 0.0, r, 1.0)
        sin_t = np.sin(theta)
        # Unit-sphere points in the viewer frame: z points at the viewer.
        self._P = np.stack(
            [sin_t * X / safe_r, sin_t * Y / safe_r, np.cos(theta)]
        ).reshape(3, -1)  # (3, h*w)

        # Soft limb darkening, by how far round the sphere each pixel looks.
        theta_max = float(theta.max()) or 1.0
        self._shade = 1.0 - edge_dim * (theta / theta_max) ** 2  # (h, w)

        t = math.radians(tilt_deg)
        self._tilt = np.array(
            [
                [1.0, 0.0, 0.0],
                [0.0, math.cos(t), -math.sin(t)],
                [0.0, math.sin(t), math.cos(t)],
            ]
        )

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
        t5 = _wave(T(tf * 11.3)) * PI2  # z shift

        # Tilt into the sphere's frame, then spin about its (vertical) axis.
        a = T(self._spin_period) * PI2
        ca, sa = math.cos(a), math.sin(a)
        spin = np.array([[ca, 0.0, sa], [0.0, 1.0, 0.0], [-sa, 0.0, ca]])
        Q = (spin @ self._tilt @ self._P) * self._sphere_radius
        x, y, zc = (c.reshape(self._h, self._w) for c in Q)

        # Same range as honeycomb's (1 + sin + cos) / 2, with a third axis.
        s = np.sin(x * f + t1) + np.cos(y * f + t2) + np.sin(zc * f + t5)
        z = (1.0 + s * (2.0 / 3.0)) * 0.5
        v = _wave(z + t4)
        v = v * v * v * self._shade
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
        if arr is None:
            return
        if self._brightness < 1.0:
            arr = (arr * self._brightness).astype(np.uint8)
        canvas.SetImage(Image.fromarray(arr))
