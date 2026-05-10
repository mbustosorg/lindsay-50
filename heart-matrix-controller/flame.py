import random
import time
import displayio
import bitmaptools

_PALETTE_SIZE = 32

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


class Flame:
    def __init__(self, display, group, frame_delay=0.05, ignite_prob=0.3,
                 max_brightness=0.4, scale=2):
        self.display = display
        self.frame_delay = frame_delay
        self.ignite_prob = ignite_prob
        self.last_frame = 0.0

        # Run the heat field at 1/scale resolution and let displayio upscale,
        # so per-frame work drops by scale**2 (here 4x).
        self.w = display.width // scale
        self.h = display.height // scale

        self.bitmap = displayio.Bitmap(self.w, self.h, _PALETTE_SIZE)
        self.palette = displayio.Palette(_PALETTE_SIZE)
        self.palette[0] = 0x000000
        for i in range(1, _PALETTE_SIZE):
            c = _heat_color(i / (_PALETTE_SIZE - 1))
            # Cap the palette to keep total LED current draw within USB budget;
            # without this the panel browns out as heat fills the screen.
            r = int(((c >> 16) & 0xFF) * max_brightness)
            g = int(((c >> 8) & 0xFF) * max_brightness)
            b = int((c & 0xFF) * max_brightness)
            self.palette[i] = (r << 16) | (g << 8) | b

        self.tilegrid = displayio.TileGrid(self.bitmap, pixel_shader=self.palette)
        wrapper = displayio.Group(scale=scale)
        wrapper.append(self.tilegrid)
        group.insert(0, wrapper)

        self._original_palette = [self.palette[i] for i in range(_PALETTE_SIZE)]
        self._buf = bytearray(self.w * self.h)

    def set_brightness(self, b):
        for i, c in enumerate(self._original_palette):
            r = int(((c >> 16) & 0xFF) * b)
            g = int(((c >> 8) & 0xFF) * b)
            bl = int((c & 0xFF) * b)
            self.palette[i] = (r << 16) | (g << 8) | bl

    def tick(self):
        now = time.monotonic()
        if now - self.last_frame < self.frame_delay:
            return
        self.last_frame = now

        w, h = self.w, self.h
        buf = self._buf

        # Heat rises one row (one memmove on the bytearray).
        buf[: w * (h - 1)] = buf[w:]

        # Cool every cell by 1 so columns fade as they rise.
        for i in range(w * h):
            v = buf[i]
            if v:
                buf[i] = v - 1

        # Sparse hot ignitions along the bottom.
        bottom = (h - 1) * w
        hot_min = _PALETTE_SIZE - 4
        for x in range(w):
            if random.random() < self.ignite_prob:
                buf[bottom + x] = hot_min + random.getrandbits(2)
            else:
                buf[bottom + x] = 0

        bitmaptools.arrayblit(self.bitmap, buf, 0, 0, w, h)
