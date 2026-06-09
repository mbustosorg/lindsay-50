
import math
import random
import time
from rgb_display import Bitmap, Palette, Effect, arrayblit

_PALETTE_SIZE = 64

# Piecewise-linear keypoints for the heat → color ramp.  Adjust freely; each
# entry is (t in [0,1], R, G, B).  Order matters and t must be ascending.
_HEAT_KEYPOINTS = (
    (0.00, 0, 0, 0),         # black (no heat)
    (0.10, 30, 0, 30),        # deep purple (almost-extinguished embers)
    (0.22, 110, 0, 30),       # plum red
    (0.35, 220, 20, 0),       # bright red
    (0.50, 255, 100, 0),      # orange
    (0.65, 255, 200, 0),      # yellow-orange
    (0.80, 255, 255, 90),     # bright yellow
    (0.92, 220, 255, 230),    # near-white
    (1.00, 180, 220, 255),    # cyan-white (hottest core)
)


def _heat_color(t):
    """Piecewise-linear interpolation across _HEAT_KEYPOINTS."""
    if t <= 0:
        return 0
    if t >= 1:
        _, r, g, b = _HEAT_KEYPOINTS[-1]
        return (r << 16) | (g << 8) | b
    for i in range(len(_HEAT_KEYPOINTS) - 1):
        t1, r1, g1, b1 = _HEAT_KEYPOINTS[i]
        t2, r2, g2, b2 = _HEAT_KEYPOINTS[i + 1]
        if t <= t2:
            u = (t - t1) / (t2 - t1)
            r = int(r1 + (r2 - r1) * u)
            g = int(g1 + (g2 - g1) * u)
            b = int(b1 + (b2 - b1) * u)
            return (r << 16) | (g << 8) | b
    return 0


class Flame(Effect):
    # Heat field runs at full 8-bit resolution (0..255) for smooth gradients,
    # then is mapped down to the palette's index range when blitted.
    _HEAT_MAX = 255

    def __init__(self, display, frame_delay=0.05, cooling=None, fuel_min=165,
                 max_brightness=0.7, scale=1, wind_speed=0.4):
        self.display = display
        self.frame_delay = frame_delay
        self.last_frame = 0.0
        self.scale = scale

        # Slowly drifting wind makes the whole flame lean and sway over time.
        self.wind_speed = wind_speed
        self._wind_phase = 0.0

        # Run the heat field at 1/scale resolution and let the renderer upscale,
        # so per-frame work drops by scale**2.
        self.w = display.width // scale
        self.h = display.height // scale

        # Persistent hot fuel bed flickers between fuel_min..255 each frame.
        self._fuel_min = fuel_min
        # Per-cell random cooling, auto-scaled to panel height: a cell loses on
        # average cool_max/2 of heat per row it rises, so flames burn out near
        # the top regardless of how tall the panel is (taller -> gentler).
        self._cool_max = cooling if cooling is not None else max(3, 640 // self.h)

        self.bitmap = Bitmap(self.w, self.h, _PALETTE_SIZE)
        self.palette = Palette(_PALETTE_SIZE)
        self.palette[0] = 0x000000
        for i in range(1, _PALETTE_SIZE):
            c = _heat_color(i / (_PALETTE_SIZE - 1))
            # Cap the palette to keep total LED current draw within USB budget;
            # without this the panel browns out as heat fills the screen.
            r = int(((c >> 16) & 0xFF) * max_brightness)
            g = int(((c >> 8) & 0xFF) * max_brightness)
            b = int((c & 0xFF) * max_brightness)
            self.palette[i] = (r << 16) | (g << 8) | b

        self._init_render()
        self._heat = bytearray(self.w * self.h)  # 0..255 heat field
        self._buf = bytearray(self.w * self.h)   # palette indices for the blit

    def tick(self):
        now = time.monotonic()
        if now - self.last_frame < self.frame_delay:
            return
        self.last_frame = now

        w, h = self.w, self.h
        heat = self._heat
        rnd = random.getrandbits
        cool_max = self._cool_max

        # Drift the wind so flames lean and sway instead of rising dead straight.
        # Two out-of-phase sines beat for a non-repetitive gust; round to a whole
        # column of lean (-1, 0, or 1) shared by the whole frame.
        self._wind_phase += self.frame_delay * self.wind_speed
        gust = math.sin(self._wind_phase) + 0.4 * math.sin(self._wind_phase * 2.7)
        wind_off = int(round(gust))

        # Diffuse heat upward (toward y=0) and blur it sideways, then cool each
        # cell a random amount. Averaging the cells below makes flames smooth and
        # tapered; the random cooling makes them flicker with dark licking tips.
        last = w - 1
        for y in range(h - 1):
            row = y * w
            b1 = row + w                          # one row below (y+1)
            b2 = b1 + w if y + 2 < h else b1       # two rows below, clamped at base
            for x in range(w):
                sx = x - wind_off                  # lean the source column upwind
                if sx < 0:
                    sx = 0
                elif sx > last:
                    sx = last
                lx = sx - 1 if sx > 0 else 0
                rx = sx + 1 if sx < last else last
                # Mostly straight up, a little from the diagonals -> rising tongues.
                avg = (heat[b1 + sx] * 4 + heat[b2 + sx] * 2
                       + heat[b1 + lx] + heat[b1 + rx]) >> 3
                cool = (rnd(8) * cool_max) >> 8    # 0..cool_max-1, ~uniform
                v = avg - cool
                heat[row + x] = v if v > 0 else 0

        # Fuel bed: keep the bottom row hot with smooth flicker (no on/off
        # strobing) so the diffusion above always has a glowing base to draw from.
        base = (h - 1) * w
        fuel_min = self._fuel_min
        fuel_span = self._HEAT_MAX - fuel_min
        for x in range(w):
            heat[base + x] = fuel_min + ((rnd(8) * fuel_span) >> 8)

        # Map 0..255 heat down to the 0..(_PALETTE_SIZE-1) palette and blit.
        buf = self._buf
        shift = 8 - (_PALETTE_SIZE.bit_length() - 1)  # 255 -> _PALETTE_SIZE-1
        for i in range(w * h):
            buf[i] = heat[i] >> shift
        arrayblit(self.bitmap, buf, 0, 0, w, h)
