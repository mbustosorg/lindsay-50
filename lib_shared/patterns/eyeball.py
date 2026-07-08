"""Eyeball — a port of a Pixelblaze 2D pattern (gazing eye).

A single eye: white sclera, a colored iris with a soft (feathered) edge, a
limbal ring, faint radial spokes, and a sharp black pupil. The gaze wanders —
every so often it eases toward a fresh random point inside the unit disk, at a
random speed. The easing is frame-rate independent: it advances by the real
per-frame time delta, exactly like Pixelblaze's `beforeRender(delta)`.

Full-color per-pixel (rgb/hsv), so like honeycomb / windfire it computes a
whole RGB frame and blits it with canvas.SetImage(), vectorized with numpy.

Pixelblaze primitives, mapped:
    beforeRender(delta)  delta = ms since last frame -> real monotonic delta
    prng(max)            uniform random in [0, max)  -> random.random()*max
    smoothstep(e0,e1,x)  Hermite edge ramp, clamped to [0,1]
    hsv(h,s,v)           HSV->RGB (reused from honeycomb)
    clamp / lerp         elementwise
"""

import logging
import math
import random
import time

import numpy as np

from lib_shared.effect_base import Effect
from lib_shared.patterns.honeycomb import _hsv_to_rgb

logger = logging.getLogger("heart")

PI2 = 2.0 * math.pi


def _smoothstep(edge0, edge1, x):
    t = np.clip((x - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


class Eyeball(Effect):
    """Wandering-gaze eye, blitted via canvas.SetImage()."""

    # Eye geometry, in normalized [0,1] panel coordinates.
    _EYE_R = 0.38
    _IRIS_R = 0.15
    _PUPIL_R = 0.07
    _IRIS_FEATHER = 0.03  # softness of the iris edge
    _CX = 0.5
    _CY = 0.49

    def __init__(
        self,
        display,
        iris_hue=0.58,
        iris_sat=0.85,
        pick_every_ms=1000.0,
        max_brightness=0.7,
    ):
        self._w = display.canvas.width
        self._h = display.canvas.height
        self._iris_hue = iris_hue
        self._iris_sat = iris_sat
        self._max_brightness = max_brightness
        self._brightness = 1.0

        # Gaze easing state (mirrors the Pixelblaze globals).
        self._gaze_x = 0.0
        self._gaze_y = 0.0
        self._target_x = 0.0
        self._target_y = 0.0
        self._counter = 0.0
        self._pick_every = pick_every_ms
        self._ease = 20.0
        # How far the iris can travel from center before its edge would leave
        # the sclera — the gaze offset is scaled by this.
        self._max_move = self._EYE_R - self._IRIS_R - 0.03

        self._last = time.monotonic()

        # Static geometry (gaze-independent): pixel offsets from the eye center,
        # the distance field, and the pre-shaded sclera (white, dimmed toward
        # the rim and under a soft lower-eyelid shadow).
        w1 = max(self._w - 1, 1)
        h1 = max(self._h - 1, 1)
        xs = np.arange(self._w) / w1
        ys = np.arange(self._h) / h1
        X, Y = np.meshgrid(xs, ys)  # (h, w)
        self._px = X - self._CX
        self._py = Y - self._CY
        self._d_eye = np.hypot(self._px, self._py)
        self._eye_mask = self._d_eye <= self._EYE_R
        shadow = 1.0 - 0.18 * np.clip((Y - 0.78) / 0.22, 0.0, 1.0)
        sclera_v = (1.0 - 0.35 * (self._d_eye / self._EYE_R)) * shadow
        self._sclera_rgb = np.repeat(sclera_v[..., None], 3, axis=-1)  # (h, w, 3)

        self._frame = None
        self._compute()

    def _pick_target(self):
        # Uniform point in the unit disk: angle uniform, radius = sqrt(uniform).
        a = random.random() * PI2
        r = math.sqrt(random.random())
        self._target_x = math.cos(a) * r
        self._target_y = math.sin(a) * r

    def tick(self):
        now = time.monotonic()
        delta_ms = (now - self._last) * 1000.0
        self._last = now

        self._counter += delta_ms
        if self._counter > self._pick_every:
            self._counter = 0.0
            self._pick_every = random.random() * 5000.0
            self._pick_target()
            self._ease = 8.0 + random.random() * 12.0

        # Frame-rate independent exponential ease toward the target.
        t = 1.0 - math.exp(-self._ease * delta_ms / 2000.0)
        self._gaze_x += (self._target_x - self._gaze_x) * t
        self._gaze_y += (self._target_y - self._gaze_y) * t

        self._compute()

    def _compute(self):
        irisR = self._IRIS_R
        feather = self._IRIS_FEATHER

        # Iris coordinates: the sclera stays put, the iris slides with the gaze.
        ix = self._px - self._gaze_x * self._max_move
        iy = self._py - self._gaze_y * self._max_move
        d_iris = np.hypot(ix, iy)

        # Soft iris edge (1 inside, ramps to 0 across the feather band).
        iris_mask = 1.0 - _smoothstep(irisR - feather, irisR + feather, d_iris)

        # Darker limbal ring toward the edge + subtle radial spokes.
        rim = _smoothstep(irisR * 0.7, irisR, d_iris)
        spokes = 0.12 * np.sin(38.0 * np.arctan2(iy, ix) + 8.0 * d_iris)
        v = np.clip(0.85 - 0.35 * rim + spokes, 0.0, 1.0)

        h = np.full_like(v, self._iris_hue)
        iris_rgb = _hsv_to_rgb(h, self._iris_sat, v)  # (h, w, 3)

        # Blend iris into the sclera by the soft mask.
        rgb = self._sclera_rgb + (iris_rgb - self._sclera_rgb) * iris_mask[..., None]

        # Sharp black pupil, then black outside the eye.
        rgb[d_iris < self._PUPIL_R] = 0.0
        rgb[~self._eye_mask] = 0.0

        rgb *= self._max_brightness
        self._frame = np.clip(rgb * 255.0, 0, 255).astype(np.uint8)

    # -- Effect interface (overridden; no Bitmap/Palette needed) --

    def set_brightness(self, b):
        self._brightness = b

    def render(self, canvas):
        from PIL import Image

        arr = self._frame
        if arr is None:
            return
        if self._brightness < 1.0:
            arr = (arr * self._brightness).astype(np.uint8)
        canvas.SetImage(Image.fromarray(arr))
