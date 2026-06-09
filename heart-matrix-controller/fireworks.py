import math
import random
import time
from rgb_display import Bitmap, Palette, Effect

# Saturated hues that read well on HUB75 panels.
_HUES = (
    0xFF0000, 0xFF6600, 0xFFCC00, 0xFFFF00, 0x66FF00,
    0x00FFAA, 0x00CCFF, 0x3366FF, 0xCC00FF, 0xFF00AA,
    0xFFFFFF,
)

_PALETTE_SIZE = 32  # index 0 = black background; 1..31 = preassigned hues


class Fireworks(Effect):
    def __init__(self, display, frame_delay=0.05, spawn_seconds=0.8,
                 sparks_per_burst=18, gravity=0.15):
        self.display = display
        self.frame_delay = frame_delay
        self.spawn_seconds = spawn_seconds
        self.sparks_per_burst = sparks_per_burst
        self.gravity = gravity
        self.last_frame = 0.0
        self.last_spawn = 0.0

        self.bitmap = Bitmap(display.width, display.height, _PALETTE_SIZE)
        self.palette = Palette(_PALETTE_SIZE)
        self.palette[0] = 0x000000
        for i in range(1, _PALETTE_SIZE):
            self.palette[i] = random.choice(_HUES)

        self._init_render()

        # Particle = [x, y, vx, vy, life, color_idx, kind]  kind: 0=rocket, 1=spark
        self.particles = []

    def tick(self):
        now = time.monotonic()
        if now - self.last_frame < self.frame_delay:
            return
        self.last_frame = now

        self.bitmap.fill(0)

        if now - self.last_spawn > self.spawn_seconds:
            self._spawn_rocket()
            self.last_spawn = now

        w, h = self.display.width, self.display.height
        survivors = []
        explosions = []

        for p in self.particles:
            p[0] += p[2]
            p[1] += p[3]
            p[3] += self.gravity
            p[4] -= 1

            if p[6] == 0 and p[3] >= 0:
                explosions.append((p[0], p[1], p[5]))
                continue

            x, y = int(p[0]), int(p[1])
            if p[4] > 0 and 0 <= x < w and 0 <= y < h:
                self.bitmap[x, y] = p[5]
                survivors.append(p)

        for ex, ey, color_idx in explosions:
            for _ in range(self.sparks_per_burst):
                angle = random.uniform(0, 6.2832)
                speed = random.uniform(0.5, 1.8)
                survivors.append([
                    ex, ey,
                    math.cos(angle) * speed,
                    math.sin(angle) * speed,
                    # Long enough for sparks to fall through both panels.
                    random.randint(45, 75),
                    color_idx,
                    1,
                ])

        self.particles = survivors

    def _spawn_rocket(self):
        # vy_init chosen so apex lands in the upper panel (rows 0..31):
        # apex_y = launch_y - vy_init**2 / (2*gravity)
        self.particles.append([
            random.randint(8, self.display.width - 8),
            self.display.height - 1,
            random.uniform(-0.3, 0.3),
            random.uniform(-4.2, -3.6),
            80,
            random.randint(1, _PALETTE_SIZE - 1),
            0,
        ])
