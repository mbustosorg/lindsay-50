import math
import random
import time
import displayio

_PALETTE_SIZE = 32


class NightSky:
    def __init__(self, display, group, frame_delay=0.05, num_stars=45,
                 shoot_min=5.0, shoot_max=15.0, sky_color=0x000000,
                 twinkle_period=12.0, twinkle_duration=0.5):
        self.display = display
        self.frame_delay = frame_delay
        self.shoot_min = shoot_min
        self.shoot_max = shoot_max
        # Mean seconds between twinkles per star, expressed as a per-tick chance.
        self._twinkle_prob = frame_delay / twinkle_period
        self._twinkle_duration = twinkle_duration
        self.last_frame = 0.0

        self.w = display.width
        self.h = display.height

        self.bitmap = displayio.Bitmap(self.w, self.h, _PALETTE_SIZE)
        self.palette = displayio.Palette(_PALETTE_SIZE)
        self.palette[0] = sky_color
        # Star intensity gradient with a slight cool-white cast.
        for i in range(1, _PALETTE_SIZE):
            t = i / (_PALETTE_SIZE - 1)
            r = int(255 * t * 0.88)
            g = int(255 * t * 0.95)
            b = int(255 * t)
            self.palette[i] = (r << 16) | (g << 8) | b

        self.tilegrid = displayio.TileGrid(self.bitmap, pixel_shader=self.palette)
        group.insert(0, self.tilegrid)

        self._original_palette = [self.palette[i] for i in range(_PALETTE_SIZE)]

        # Star = [x, y, base, amp, twinkle_start, twinkle_duration]
        # twinkle_start < 0 means the star is idle; otherwise it's the wall-clock
        # time the current pulse began and twinkle_duration is its length.
        self.stars = []
        for _ in range(num_stars):
            self.stars.append([
                random.randint(0, self.w - 1),
                random.randint(0, self.h - 1),
                random.randint(4, 10),
                random.randint(12, 20),
                -1.0,
                twinkle_duration,
            ])

        # Shoot = [x, y, vx, vy, trail_len, head_intensity]  (None = idle)
        self.shoot = None
        self.next_shoot = time.monotonic() + random.uniform(shoot_min, shoot_max)

    def set_brightness(self, b):
        for i, c in enumerate(self._original_palette):
            r = int(((c >> 16) & 0xFF) * b)
            g = int(((c >> 8) & 0xFF) * b)
            bl = int((c & 0xFF) * b)
            self.palette[i] = (r << 16) | (g << 8) | bl

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

        self.bitmap.fill(0)

        # Twinkle each star: idle stars sit at base intensity; with low per-tick
        # probability, an idle star starts a half-second sin-shaped pulse that
        # rises to base+amp and returns.  Already-pulsing stars advance through
        # their pulse and clear themselves when done.
        for s in self.stars:
            base = s[2]
            if s[4] < 0:
                if random.random() < self._twinkle_prob:
                    s[4] = now
                    s[5] = self._twinkle_duration * random.uniform(0.8, 1.2)
                v = base
            else:
                elapsed = now - s[4]
                if elapsed >= s[5]:
                    s[4] = -1.0
                    v = base
                else:
                    v = base + int(math.sin(math.pi * elapsed / s[5]) * s[3])
                    if v >= _PALETTE_SIZE:
                        v = _PALETTE_SIZE - 1
            if v < 1:
                v = 1
            self.bitmap[s[0], s[1]] = v

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
                    if v > self.bitmap[tx, ty]:
                        self.bitmap[tx, ty] = v
            sh[0] += sh[2]
            sh[1] += sh[3]
            if sh[0] < -15 or sh[0] > self.w + 15 or sh[1] > self.h + 15:
                self.shoot = None
                self.next_shoot = now + random.uniform(self.shoot_min, self.shoot_max)