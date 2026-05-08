import random
import time
import displayio
import bitmaptools

_PALETTE_SIZE = 32


def _heat_color(t):
    """t in [0,1] mapped through black -> red -> orange -> yellow -> white."""
    if t < 0.25:
        return int(255 * t / 0.25) << 16
    if t < 0.5:
        return (255 << 16) | (int(128 * (t - 0.25) / 0.25) << 8)
    if t < 0.75:
        return (255 << 16) | (int(128 + 127 * (t - 0.5) / 0.25) << 8)
    return (255 << 16) | (255 << 8) | int(255 * (t - 0.75) / 0.25)


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
