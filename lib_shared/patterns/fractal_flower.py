"""Fractal Flower — a port of a Pixelblaze 2D pattern (Ben Hencke, 2021).

A recursive fractal paints glowing "petals" into a small square accumulator
buffer: from a handful of seed points arranged on a ring, a binary tree of
branches fans out, each branch rotating by one of two time-varying angles and
stepping a shrinking distance. Wherever a branch lands it deposits brightness
and blends in a hue; the whole buffer fades a little every step, so the petals
leave comet-like trails as the branch angles sweep. Two seed layouts are
supported — a pinwheel (each petal spins in place) and petals orbiting the
centre.

Like honeycomb / windfire / metaballs this paints per-pixel HSV, so it keeps
the fractal state in a numpy buffer and blits a whole RGB frame with
canvas.SetImage() rather than per-pixel SetPixel().

The Pixelblaze source also bolts a 7-segment clock overlay on top of this
background; that half is intentionally NOT ported — this display cycles
backgrounds behind scrolling text and has no clock concept, and the overlay's
edge geometry was hardcoded for a different panel.

Pixelblaze primitives, mapped:
    time(i)     sawtooth 0..1, period i*65.536s (via windfire._time helpers)
    wave(v)     (1 + sin(v*2pi)) / 2
    hsv(h,s,v)  HSV->RGB, hue wraps mod 1
    array.mutate(p => p*fade)   whole-buffer fade -> vectorized numpy multiply

Fidelity notes:
  * The recursion, the two-angle branching, and the brightness-weighted hue
    blend (`blendHue`) are faithful to the original.
  * The original ran its fade + fractal deposit once per Pixelblaze frame, so
    the trail length tracked that device's FPS. Here the sim is advanced at a
    fixed step off the real clock (like metaballs), so trails and motion stay
    consistent regardless of the Pi's frame rate. A long stall drops its
    backlog rather than fast-forwarding across later frames.
  * `valueFactor` auto-brightness (scale the buffer by a smoothed running max
    so overlapping deposits don't clip) is kept — it's what gives the pattern
    its dynamic range.
"""

import logging
import time

import numpy as np

from lib_shared.effect_base import Effect

# Reuse the shaping + colour helpers rather than duplicating them.
from lib_shared.patterns.windfire import _wave, _TIME_UNIT
from lib_shared.patterns.honeycomb import _hsv_to_rgb

logger = logging.getLogger("heart")

PI = np.pi
PI2 = 2.0 * np.pi

# Advance the fade + fractal deposit at this fixed rate so trail length and
# motion don't scale with the Pi's frame rate (mirrors metaballs' approach).
_STEP_S = 1.0 / 30.0
# Cap sim steps per frame; a longer stall drops its backlog (see tick()).
_MAX_STEPS = 4


class FractalFlower(Effect):
    """Recursive fractal petals with fade-trails, blitted via SetImage()."""

    def __init__(
        self,
        display,
        tf=1.0,
        iterations=8,
        draw_levels=7,
        scale=0.045,
        speed=18.0,
        fade=0.9,
        angle_range1=0.4,
        angle_range2=0.4,
        replicas=7,
        spacing=0.26,
        contrast=1.1,
        use_white=False,
        use_pinwheel=True,
        wrap_world=False,
        src_size=24,
        max_brightness=0.9,
    ):
        self._w = display.canvas.width
        self._h = display.canvas.height
        # tf scales the animation clock: <1 slows it down, >1 speeds it up.
        self._tf = tf
        self._iterations = max(1, int(iterations))
        self._draw_levels = max(1, int(draw_levels))
        self._scale = scale
        self._speed = max(1e-3, speed)
        self._fade = fade
        self._angle_range1 = angle_range1
        self._angle_range2 = angle_range2
        self._replicas = max(1, int(replicas))
        self._spacing = spacing
        # Tone curve applied to the normalized value. The Pixelblaze original
        # hardcodes v*v (contrast == 2), which crushes the petal body on this
        # panel; a gentler default keeps the trails readable while still adding
        # punch. Set to 2.0 to match the original exactly.
        self._contrast = contrast
        self._use_white = use_white
        self._use_pinwheel = use_pinwheel
        self._wrap_world = wrap_world
        self._n = max(2, int(src_size))  # source matrix is _n x _n
        self._max_brightness = max_brightness
        self._brightness = 1.0

        # Fractal accumulator: one running value + hue per source cell.
        self._pixels = np.zeros(self._n * self._n, dtype=np.float64)
        self._hues = np.zeros(self._n * self._n, dtype=np.float64)
        # Auto-brightness divisor (Pixelblaze seeds this at 20).
        self._value_factor = 20.0

        # Per-step branch angles + colour, refreshed from the clock each step.
        self._color = 0.0
        self._branch1 = 0.0
        self._branch2 = 0.0

        # Fixed-step accumulator off the real clock.
        self._t0 = time.monotonic()
        self._last = self._t0
        self._accum = 0.0

        # Precompute each display pixel's source-cell index. Normalized 0..1
        # per axis (row 0 = top), mapped into the _n x _n square exactly like
        # the original getIndex(); clamp the last row/col off the edge.
        n = self._n
        w1 = max(self._w - 1, 1)
        h1 = max(self._h - 1, 1)
        xn = np.arange(self._w) / w1
        yn = np.arange(self._h) / h1
        xi = np.minimum((xn * n).astype(np.int32), n - 1)
        yi = np.minimum((yn * n).astype(np.int32), n - 1)
        gx, gy = np.meshgrid(xi, yi)  # (h, w)
        self._src_index = gy * n + gx  # (h, w) int

        self._frame = None
        self._build_frame()

    # -- Fractal core (faithful to the Pixelblaze recursion) --

    def _fractal(self, x, y, a, i):
        """Recurse one branch: step, deposit, then spawn two sub-branches.

        `i` counts down from `iterations` to 1. The first (root) call doesn't
        move; deeper calls step a shrinking distance `i*scale + scale` along the
        branch angle. A cell is drawn only once `i <= draw_levels` and the point
        is on-screen — skipping the earliest levels hides the bare stem."""
        n = self._n
        if i < self._iterations:
            length = i * self._scale + self._scale
            x += np.sin(a) * length
            y += np.cos(a) * length

        if self._wrap_world:
            x %= 0.99999
            y %= 0.99999

        if i <= self._draw_levels and 0.0 <= x <= 0.99999 and 0.0 <= y <= 0.999999:
            idx = int(x * n) + int(y * n) * n
            self._blend(idx, i * 0.1 + self._color)

        i -= 1
        if i > 0:
            self._fractal(x, y, a + self._branch1, i)
            self._fractal(x, y, a + self._branch2, i)

    def _blend(self, idx, new_hue):
        """Deposit +1 brightness at `idx` and blend `new_hue` in, weighted by
        the running brightness (Pixelblaze `blendHue` with v2 == 1)."""
        h1 = self._hues[idx]
        v1 = self._pixels[idx]
        total_v = v1 + 1.0
        # Rotate the two hues so they're numerically adjacent before averaging,
        # so a blend across the 1.0/0.0 wrap doesn't average the long way round.
        h2 = new_hue
        if h2 - h1 > 0.5:
            h2 -= 1.0
        if h1 - h2 > 0.5:
            h1 -= 1.0
        self._hues[idx] = (h1 * v1 + h2) / total_v
        self._pixels[idx] = total_v

    def _step(self):
        """Advance the sim one fixed step: refresh angles, fade, deposit."""
        speed = self._speed
        self._color = self._time(1.0 / speed)
        self._branch1 = -1.0 + np.sin(_wave(self._time(4.4 / speed)) * PI2) * PI * self._angle_range1
        self._branch2 = 0.5 + np.sin(_wave(-self._time(11.0 / speed)) * PI2) * PI * self._angle_range2
        starting_angle = np.sin(self._time(3.0 / speed) * PI2) * PI

        # Fade the whole buffer so deposits leave trails.
        self._pixels *= self._fade

        r = self._replicas
        if r > 1:
            for i in range(r):
                frac = i / r
                if self._use_pinwheel:
                    # Petal spins in place around its own ring position.
                    cx = 0.5 + self._spacing * np.sin(frac * PI2)
                    cy = 0.5 + self._spacing * np.cos(frac * PI2)
                    self._fractal(cx, cy, starting_angle + frac * PI2, self._iterations)
                else:
                    # Petals orbit the centre.
                    cx = 0.5 + self._spacing * np.sin(frac * PI2 + starting_angle)
                    cy = 0.5 + self._spacing * np.cos(frac * PI2 + starting_angle)
                    self._fractal(cx, cy, frac * PI2, self._iterations)
        else:
            self._fractal(0.5, 0.5, starting_angle, self._iterations)

    def _time(self, interval):
        """Pixelblaze time(interval): sawtooth 0..1 with period interval*65.536s."""
        now = (time.monotonic() - self._t0) * self._tf
        return (now / (interval * _TIME_UNIT)) % 1.0

    def _build_frame(self):
        """Sample the accumulator into a display-sized RGB frame."""
        vals = self._pixels[self._src_index]  # (h, w)
        hue = self._hues[self._src_index]  # (h, w)

        # Gradually track the brightest cell so overlapping deposits don't clip;
        # smoothed over time to avoid flicker (Pixelblaze valueFactor).
        max_v = float(self._pixels.max())
        self._value_factor = min(100.0, max(1.0, self._value_factor * 0.95 + max_v * 0.05))

        v = np.clip(vals / self._value_factor, 0.0, 1.0)
        v = v**self._contrast  # tone curve (2.0 == the original's v*v)
        # useWhite shifts bright pixels toward white (lower saturation).
        s = (1.0 - v) if self._use_white else np.ones_like(v)

        rgb = _hsv_to_rgb(hue, s, v) * self._max_brightness
        self._frame = np.clip(rgb * 255.0, 0, 255).astype(np.uint8)  # (h, w, 3)

    # -- Effect interface (overridden; no Bitmap/Palette needed) --

    def set_brightness(self, b):
        self._brightness = b

    def tick(self):
        now = time.monotonic()
        dt = (now - self._last) * self._tf
        self._last = now
        self._accum += dt
        steps = 0
        while self._accum >= _STEP_S and steps < _MAX_STEPS:
            self._step()
            self._accum -= _STEP_S
            steps += 1
        # Hitting the cap means a real stall dumped a large backlog into _accum;
        # drop the remainder so a long pause costs at most this one burst frame
        # instead of fast-forwarding the fractal across the following frames.
        if steps == _MAX_STEPS:
            self._accum = 0.0
        self._build_frame()

    def render(self, canvas):
        from PIL import Image

        arr = self._frame
        if arr is None:
            return
        if self._brightness < 1.0:
            arr = (arr * self._brightness).astype(np.uint8)
        canvas.SetImage(Image.fromarray(arr))
