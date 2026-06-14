"""Star Wars-style hyperspace jump.

A 3D starfield projected to 2D: every star has a fixed lateral direction
(x, y) on the near plane and a depth `z`.  The screen position is the
perspective projection `center + (x, y) * scale / z`, so as `z` shrinks the
star drifts outward from the center and accelerates — exactly how stars rush
past as you fly into them.

The whole effect is driven by one `warp` factor (0 -> 1):

  * warp = 0  -> z creeps down slowly, the per-frame projected motion is sub-
    pixel, and each star renders as a calm drifting point: a starfield.
  * warp = 1  -> z plunges each frame, so a star jumps a long way across the
    screen between frames.  We draw the streak from its previous projected
    position to the new one, producing the radial light-speed stretch and the
    tunnel of streaks you ride during the jump.

A timed phase machine cycles STARFIELD -> WARP_IN -> TUNNEL -> WARP_OUT and
back, so the effect performs a full "jump and ride" on a loop.  Like the other
palette-based effects it writes intensity indices into a `Bitmap` over a blue-
white gradient palette; `Effect` supplies the brightness fade and canvas blit.
"""

import math
import random
import time

from lib_shared.effect_base import Bitmap, Palette, Effect, arrayblit

_PALETTE_SIZE = 32

# Depth range.  Stars spawn far (near ZMAX) and are recycled once they pass the
# viewer (z <= ZMIN) or their projection leaves the screen.
_ZMAX = 4.0
_ZMIN = 0.08
_SPAWN_ZMIN = 3.0

# Depth speed in z-units/second.  Total speed = base + warp * extra, so warp
# scales the jump from a gentle drift to a full light-speed plunge.
_BASE_SPEED = 0.30
_WARP_SPEED = 7.0

# Phase durations (seconds): calm field, accelerate, ride the tunnel, decelerate.
# The warp_in/warp_out ramps are kept short so the jump in and drop out feel snappy.
_T_STARFIELD = 6.0
_T_WARP_IN = 0.8
_T_TUNNEL = 8.0
_T_WARP_OUT = 1.0


def _smoothstep(t):
    """Ease 0->1 with zero slope at both ends (3t^2 - 2t^3)."""
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    return t * t * (3.0 - 2.0 * t)


class Hyperspace(Effect):
    def __init__(
        self,
        display,
        num_stars=80,
        frame_max_dt=0.1,
        core_color=(180, 215, 255),
        deep_color=(0, 10, 120),
    ):
        self.display = display
        self.w = display.canvas.width
        self.h = display.canvas.height
        # Project so a star at z = 1 lands near the panel edge; closer stars
        # fly off-screen.  Half-width drives the spread.  NOTE: this is the
        # perspective scale, deliberately NOT `self.scale` — that name belongs
        # to Effect.render()'s pixel-doubling factor (must stay 1 here, else
        # every pixel renders as a giant block).
        self.cx = (self.w - 1) / 2.0
        self.cy = (self.h - 1) / 2.0
        self.proj_scale = max(self.cx, self.cy)
        self.frame_max_dt = frame_max_dt

        self.bitmap = Bitmap(self.w, self.h)
        self.palette = Palette(_PALETTE_SIZE)
        self.palette[0] = 0x000000
        # Deep blue (faint, distant) -> blue-white (near / streak head).
        dr, dg, db = deep_color
        cr, cg, cb = core_color
        for i in range(1, _PALETTE_SIZE):
            t = i / (_PALETTE_SIZE - 1)
            e = t**0.7  # bias toward the bright end so streaks read crisp
            r = int(dr + (cr - dr) * e)
            g = int(dg + (cg - dg) * e)
            b = int(db + (cb - db) * e)
            self.palette[i] = (r << 16) | (g << 8) | b
        self._init_render()

        # Off-screen render buffer (blitted in one shot to avoid tearing).
        self._buf = bytearray(self.w * self.h)
        self._zero_buf = bytes(self.w * self.h)

        # Star = [x, y, z, sx, sy] — lateral direction, depth, last screen pos.
        self.stars = [self._new_star(spread=True) for _ in range(num_stars)]

        self.warp = 0.0
        self.phase = "starfield"
        self.phase_start = time.monotonic()
        self.last_frame = self.phase_start

    def _new_star(self, spread=False):
        """A fresh star heading out from the center in a random direction.

        `spread=True` seeds depth across the whole range (used once at startup
        so the field is already populated); otherwise it spawns far away.
        """
        star = [0.0, 0.0, 0.0, 0.0, 0.0]
        self._respawn(star, spread=spread)
        return star

    def _respawn(self, star, spread=False):
        """Reset a star in place to a new outward direction and far depth."""
        ang = random.uniform(0.0, 2.0 * math.pi)
        # Keep direction off dead-center so the star actually travels outward.
        rad = random.uniform(0.25, 1.0)
        x = math.cos(ang) * rad
        y = math.sin(ang) * rad
        z = random.uniform(_ZMIN, _ZMAX) if spread else random.uniform(_SPAWN_ZMIN, _ZMAX)
        sx, sy = self._project(x, y, z)
        star[0] = x
        star[1] = y
        star[2] = z
        star[3] = sx
        star[4] = sy

    def _project(self, x, y, z):
        f = self.proj_scale / z
        return self.cx + x * f, self.cy + y * f

    def _intensity(self, z):
        """Closer stars are brighter; far ones fade toward the floor."""
        t = (_ZMAX - z) / (_ZMAX - _ZMIN)
        if t < 0.0:
            t = 0.0
        elif t > 1.0:
            t = 1.0
        v = 2 + int(t * (_PALETTE_SIZE - 3))
        return v

    def _draw_streak(self, x0, y0, x1, y1, i0, i1):
        """Max-blend a line from (x0,y0)@i0 to (x1,y1)@i1 into the buffer.

        Pixels outside the panel are skipped, which also clips the very long
        streaks a near-zero z can produce.
        """
        x0i = int(round(x0))
        y0i = int(round(y0))
        x1i = int(round(x1))
        y1i = int(round(y1))
        dx = x1i - x0i
        dy = y1i - y0i
        steps = max(abs(dx), abs(dy))
        w = self.w
        h = self.h
        buf = self._buf
        if steps == 0:
            if 0 <= x1i < w and 0 <= y1i < h:
                idx = y1i * w + x1i
                if i1 > buf[idx]:
                    buf[idx] = i1
            return
        for s in range(steps + 1):
            t = s / steps
            px = x0i + int(round(dx * t))
            py = y0i + int(round(dy * t))
            if 0 <= px < w and 0 <= py < h:
                v = int(i0 + (i1 - i0) * t)
                if v >= 1:
                    idx = py * w + px
                    if v > buf[idx]:
                        buf[idx] = v

    def _advance_phase(self, now):
        elapsed = now - self.phase_start
        if self.phase == "starfield":
            self.warp = 0.0
            if elapsed >= _T_STARFIELD:
                self.phase = "warp_in"
                self.phase_start = now
        elif self.phase == "warp_in":
            self.warp = _smoothstep(elapsed / _T_WARP_IN)
            if elapsed >= _T_WARP_IN:
                self.phase = "tunnel"
                self.phase_start = now
        elif self.phase == "tunnel":
            self.warp = 1.0
            if elapsed >= _T_TUNNEL:
                self.phase = "warp_out"
                self.phase_start = now
        else:  # warp_out
            self.warp = 1.0 - _smoothstep(elapsed / _T_WARP_OUT)
            if elapsed >= _T_WARP_OUT:
                self.phase = "starfield"
                self.phase_start = now

    def tick(self):
        now = time.monotonic()
        dt = now - self.last_frame
        if dt <= 0.0:
            return
        if dt > self.frame_max_dt:
            dt = self.frame_max_dt
        self.last_frame = now

        self._advance_phase(now)

        dz = (_BASE_SPEED + self.warp * _WARP_SPEED) * dt

        self._buf[:] = self._zero_buf
        scale = self.proj_scale
        cx = self.cx
        cy = self.cy
        w = self.w
        h = self.h

        for star in self.stars:
            x, y, z, psx, psy = star
            new_z = z - dz
            if new_z <= _ZMIN:
                # Passed the viewer — respawn far away.
                self._respawn(star)
                continue
            f = scale / new_z
            sx = cx + x * f
            sy = cy + y * f

            head_i = self._intensity(new_z)
            tail_i = self._intensity(z)
            self._draw_streak(psx, psy, sx, sy, tail_i, head_i)

            # Recycle once fully off-screen with a margin (gone for good);
            # otherwise commit the new depth and projected position.
            if sx < -w or sx > 2 * w or sy < -h or sy > 2 * h:
                self._respawn(star)
            else:
                star[2] = new_z
                star[3] = sx
                star[4] = sy

        arrayblit(self.bitmap, self._buf)
