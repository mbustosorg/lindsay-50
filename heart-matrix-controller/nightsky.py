import math
import random
import time
from rgb_display import Bitmap, Palette, Effect, arrayblit

_PALETTE_SIZE = 32


class NightSky(Effect):
    def __init__(self, display, frame_delay=0.05, num_stars=45,
                 shoot_min=5.0, shoot_max=15.0, sky_color=0x000000,
                 twinkle_period=3.0, twinkle_fraction=0.20):
        self.display = display
        self.frame_delay = frame_delay
        self.shoot_min = shoot_min
        self.shoot_max = shoot_max
        self.twinkle_period = twinkle_period
        # How many stars should be actively twinkling at any moment.
        self.target_twinkling = max(1, int(num_stars * twinkle_fraction))
        self.last_frame = 0.0

        self.w = display.canvas.width
        self.h = display.canvas.height

        self.bitmap = Bitmap(self.w, self.h, _PALETTE_SIZE)
        self.palette = Palette(_PALETTE_SIZE)
        self.palette[0] = sky_color
        # Star intensity gradient with a slight cool-white cast.
        for i in range(1, _PALETTE_SIZE):
            t = i / (_PALETTE_SIZE - 1)
            r = int(255 * t * 0.88)
            g = int(255 * t * 0.95)
            b = int(255 * t)
            self.palette[i] = (r << 16) | (g << 8) | b

        self._init_render()

        # Render into a bytearray and push the whole frame to the bitmap in
        # one arrayblit call.  Per-pixel bitmap writes interleaved with the
        # compositor read are what produces visible tearing/flicker.
        self._buf = bytearray(self.w * self.h)
        self._zero_buf = bytes(self.w * self.h)

        # Star = [x, y, base, amp, twinkle_start]
        # twinkle_start < 0  → star is idle (rendered at base intensity).
        # twinkle_start >= 0 → wall-clock time when this twinkle began.
        # `base` is kept above the bit_depth=5 PWM-flicker floor (index ~12)
        # so calm stars render solid.  `amp` is the extra brightness added at
        # the start of a twinkle, which then fades back to `base`; combined
        # peak (base+amp) tops out near the palette max, so the active fade
        # also stays in the stable region the whole way.
        self.stars = []
        for _ in range(num_stars):
            self.stars.append([
                random.randint(0, self.w - 1),
                random.randint(0, self.h - 1),
                random.randint(12, 18),
                random.randint(10, 14),
                -1.0,
            ])

        # Seed the initial active set with phases offset across [0, period) so
        # they aren't all peaking simultaneously on the first frame.
        now = time.monotonic()
        idle = list(range(num_stars))
        for _ in range(self.target_twinkling):
            j = random.randrange(len(idle))
            self.stars[idle.pop(j)][4] = now - random.uniform(0, twinkle_period)

        # Shoot = [x, y, vx, vy, trail_len, head_intensity]  (None = idle)
        self.shoot = None
        self.next_shoot = now + random.uniform(shoot_min, shoot_max)

    def _spawn_shoot(self):
        side = random.randint(0, 2)
        if side == 0:  # enter from left
            x, y = -3, random.randint(0, self.h // 2)
            vx = random.uniform(2.0, 3.5)
            vy = random.uniform(0.5, 1.5)
        elif side == 1:  # enter from right
            x, y = self.w + 2, random.randint(0, self.h // 2)
            vx = -random.uniform(2.0, 3.5)
            vy = random.uniform(0.5, 1.5)
        else:  # enter from top
            x, y = random.randint(0, self.w - 1), -3
            vx = random.uniform(-1.5, 1.5)
            vy = random.uniform(2.0, 3.5)
        self.shoot = [x, y, vx, vy, 10, _PALETTE_SIZE - 1]

    def tick(self):
        now = time.monotonic()
        if now - self.last_frame < self.frame_delay:
            return
        self.last_frame = now

        # Clear the off-screen render buffer (single memcpy).
        self._buf[:] = self._zero_buf

        # End any twinkle that has completed a full cycle.
        for s in self.stars:
            if s[4] >= 0 and now - s[4] >= self.twinkle_period:
                s[4] = -1.0

        # Top up the active set: pick random idle stars and start them now.
        # Each new star begins at the current wall-clock, so its phase is
        # naturally offset from already-running twinkles.
        active = sum(1 for s in self.stars if s[4] >= 0)
        if active < self.target_twinkling:
            idle = [i for i, s in enumerate(self.stars) if s[4] < 0]
            for _ in range(min(self.target_twinkling - active, len(idle))):
                j = random.randrange(len(idle))
                self.stars[idle.pop(j)][4] = now

        # Render every star into the buffer.  Idle stars sit at their calm
        # base intensity.  Active stars start at base+amp and smoothly fade
        # back to base over twinkle_period (cosine half-cycle).
        pi_over_period = math.pi / self.twinkle_period
        w = self.w
        buf = self._buf
        for s in self.stars:
            base = s[2]
            if s[4] < 0:
                v = base
            else:
                decay = (1.0 + math.cos((now - s[4]) * pi_over_period)) * 0.5
                v = base + int(s[3] * decay)
                if v >= _PALETTE_SIZE:
                    v = _PALETTE_SIZE - 1
            if v >= 1:
                buf[s[1] * w + s[0]] = v

        # Shooting star: head + fading trail along the velocity vector.
        if self.shoot is None:
            if now >= self.next_shoot:
                self._spawn_shoot()
        else:
            sh = self.shoot
            head_i = sh[5]
            for k in range(sh[4]):
                tx = int(sh[0] - sh[2] * k * 0.5)
                ty = int(sh[1] - sh[3] * k * 0.5)
                if 0 <= tx < self.w and 0 <= ty < self.h:
                    v = head_i - k * 3
                    if v < 1:
                        v = 1
                    idx = ty * w + tx
                    if v > buf[idx]:
                        buf[idx] = v
            sh[0] += sh[2]
            sh[1] += sh[3]
            if sh[0] < -15 or sh[0] > self.w + 15 or sh[1] > self.h + 15:
                self.shoot = None
                self.next_shoot = now + random.uniform(self.shoot_min, self.shoot_max)

        # One atomic update of the whole bitmap.
        arrayblit(self.bitmap, buf, 0, 0, self.w, self.h)