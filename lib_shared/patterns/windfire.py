"""WindFire — a port of a Pixelblaze 2D pattern (wind-bent perlin fire).

The Pixelblaze original renders a full-color flame driven by 3D Perlin
turbulence, with a per-pixel horizontal "wobble" that makes the flame lean
and sway as if bent by wind. Like the honeycomb / video patterns this doesn't
fit the indexed Bitmap/Palette pipeline, so it computes a whole RGB frame and
blits it with canvas.SetImage(). The per-pixel math is vectorized with numpy
to stay real-time at 64x64.

Pixelblaze primitives, mapped:
    time(i)              sawtooth 0..1, period i*65.536s
    wave(v)              (1 + sin(v*2pi)) / 2
    triangle(v)          1 - |1 - 2*frac(v)|
    perlin(x,y,z,seed)   improved (Ken Perlin) 3D noise, wraps every 256
    perlinTurbulence(...) sum of |perlin| over octaves (fractal turbulence)
    setPalette(grad)     piecewise-linear RGB ramp indexed 0..1
    paint(v, bri)        palette color at position v, scaled by brightness bri

The original cycles four noise modes but forces `mode = 3` (turbulence); this
port hardcodes turbulence to match. resetTransform/translate/scale in the
original zoom the flame 2x and center it (nodeId picks the translate); with a
single panel we use the centered translate (-0.5).
"""

import logging
import time

import numpy as np

from lib_shared.effect_base import Effect

logger = logging.getLogger("heart")

PI2 = 2.0 * np.pi

# Pixelblaze time(i) has period i * 65.536 seconds (2^16 ms).
_TIME_UNIT = 65.536

# Standard Ken Perlin permutation table, doubled to 512 so index+offset math
# never runs off the end.
_PERM = np.array(
    [
        151,
        160,
        137,
        91,
        90,
        15,
        131,
        13,
        201,
        95,
        96,
        53,
        194,
        233,
        7,
        225,
        140,
        36,
        103,
        30,
        69,
        142,
        8,
        99,
        37,
        240,
        21,
        10,
        23,
        190,
        6,
        148,
        247,
        120,
        234,
        75,
        0,
        26,
        197,
        62,
        94,
        252,
        219,
        203,
        117,
        35,
        11,
        32,
        57,
        177,
        33,
        88,
        237,
        149,
        56,
        87,
        174,
        20,
        125,
        136,
        171,
        168,
        68,
        175,
        74,
        165,
        71,
        134,
        139,
        48,
        27,
        166,
        77,
        146,
        158,
        231,
        83,
        111,
        229,
        122,
        60,
        211,
        133,
        230,
        220,
        105,
        92,
        41,
        55,
        46,
        245,
        40,
        244,
        102,
        143,
        54,
        65,
        25,
        63,
        161,
        1,
        216,
        80,
        73,
        209,
        76,
        132,
        187,
        208,
        89,
        18,
        169,
        200,
        196,
        135,
        130,
        116,
        188,
        159,
        86,
        164,
        100,
        109,
        198,
        173,
        186,
        3,
        64,
        52,
        217,
        226,
        250,
        124,
        123,
        5,
        202,
        38,
        147,
        118,
        126,
        255,
        82,
        85,
        212,
        207,
        206,
        59,
        227,
        47,
        16,
        58,
        17,
        182,
        189,
        28,
        42,
        223,
        183,
        170,
        213,
        119,
        248,
        152,
        2,
        44,
        154,
        163,
        70,
        221,
        153,
        101,
        155,
        167,
        43,
        172,
        9,
        129,
        22,
        39,
        253,
        19,
        98,
        108,
        110,
        79,
        113,
        224,
        232,
        178,
        185,
        112,
        104,
        218,
        246,
        97,
        228,
        251,
        34,
        242,
        193,
        238,
        210,
        144,
        12,
        191,
        179,
        162,
        241,
        81,
        51,
        145,
        235,
        249,
        14,
        239,
        107,
        49,
        192,
        214,
        31,
        181,
        199,
        106,
        157,
        184,
        84,
        204,
        176,
        115,
        121,
        50,
        45,
        127,
        4,
        150,
        254,
        138,
        236,
        205,
        93,
        222,
        114,
        67,
        29,
        24,
        72,
        243,
        141,
        128,
        195,
        78,
        66,
        215,
        61,
        156,
        180,
    ],
    dtype=np.int32,
)
_P = np.concatenate([_PERM, _PERM])


def _wave(v):
    return (1.0 + np.sin(v * PI2)) * 0.5


def _triangle(v):
    frac = v - np.floor(v)
    return 1.0 - np.abs(1.0 - 2.0 * frac)


def _fade(t):
    return t * t * t * (t * (t * 6.0 - 15.0) + 10.0)


def _lerp(t, a, b):
    return a + t * (b - a)


def _grad(h, x, y, z):
    """Improved-Perlin gradient dot product (12 edge gradients)."""
    h = h & 15
    u = np.where(h < 8, x, y)
    v = np.where(h < 4, y, np.where((h == 12) | (h == 14), x, z))
    return np.where(h & 1 == 0, u, -u) + np.where(h & 2 == 0, v, -v)


def _perlin(x, y, z):
    """3D improved Perlin noise. x, y are arrays; z a scalar. Wraps every 256."""
    xi = np.floor(x).astype(np.int32) & 255
    yi = np.floor(y).astype(np.int32) & 255
    zi = int(np.floor(z)) & 255
    xf = x - np.floor(x)
    yf = y - np.floor(y)
    zf = z - np.floor(z)
    u = _fade(xf)
    v = _fade(yf)
    w = _fade(zf)

    P = _P
    A = P[xi] + yi
    AA = P[A] + zi
    AB = P[A + 1] + zi
    B = P[xi + 1] + yi
    BA = P[B] + zi
    BB = P[B + 1] + zi

    x1 = _lerp(u, _grad(P[AA], xf, yf, zf), _grad(P[BA], xf - 1, yf, zf))
    x2 = _lerp(u, _grad(P[AB], xf, yf - 1, zf), _grad(P[BB], xf - 1, yf - 1, zf))
    y1 = _lerp(v, x1, x2)
    x3 = _lerp(u, _grad(P[AA + 1], xf, yf, zf - 1), _grad(P[BA + 1], xf - 1, yf, zf - 1))
    x4 = _lerp(u, _grad(P[AB + 1], xf, yf - 1, zf - 1), _grad(P[BB + 1], xf - 1, yf - 1, zf - 1))
    y2 = _lerp(v, x3, x4)
    return _lerp(w, y1, y2)


def _perlin_turbulence(x, y, z, lacunarity, gain, octaves):
    """Fractal turbulence: sum of |perlin| across octaves (Pixelblaze order)."""
    total = np.zeros_like(x)
    freq = 1.0
    amp = 1.0
    for _ in range(octaves):
        total += np.abs(_perlin(x * freq, y * freq, z * freq)) * amp
        freq *= lacunarity
        amp *= gain
    return total


# setPalette(rgbGradient): position -> (R, G, B), interpolated linearly.
_PAL_POS = np.array([0.0, 0.2, 0.9, 1.0])
_PAL_R = np.array([0.0, 1.0, 1.0, 1.0])
_PAL_G = np.array([0.0, 0.0, 0.6, 0.8])
_PAL_B = np.array([0.0, 0.0, 0.0, 0.3])


class WindFire(Effect):
    """Wind-bent Perlin fire, blitted via canvas.SetImage()."""

    # Coordinate zoom from the Pixelblaze scale(s, s).
    _S = 2.0

    def __init__(self, display, tf=1.0, max_brightness=0.7):
        self._w = display.canvas.width
        self._h = display.canvas.height
        # tf scales the animation clock: <1 slows it down, >1 speeds it up.
        self._tf = tf
        self._max_brightness = max_brightness
        self._brightness = 1.0
        self._t0 = time.monotonic()

        # Normalized 0..1 pixel coordinates (row 0 = top). Post-transform
        # coords: x' = s*(xn-0.5) centers the flame; y' = s*yn puts the hot
        # base at the bottom (larger y' -> brighter, per the original).
        w1 = max(self._w - 1, 1)
        h1 = max(self._h - 1, 1)
        xn = np.arange(self._w) / w1
        yn = np.arange(self._h) / h1
        self._Xn, self._Yn = np.meshgrid(xn, yn)  # (h, w)
        self._Xp = self._S * (self._Xn - 0.5)
        self._Yp = self._S * self._Yn

        self._frame = None
        self._compute()

    def _time(self, interval):
        """Pixelblaze time(interval): sawtooth 0..1 with period interval*65.536s."""
        now = (time.monotonic() - self._t0) * self._tf
        return (now / (interval * _TIME_UNIT)) % 1.0

    def _compute(self):
        s = self._S
        Xp = self._Xp
        Yp = self._Yp

        t2 = self._time(0.07)
        t3 = self._time(0.133)
        # perlin wraps every 256, so sweeping z across 0..256 gives smooth,
        # long-period animation without a visible seam.
        noise_time = self._time(8.0) * 256.0
        noise_y_time = self._time(1.6) * 256.0

        # Per-pixel horizontal wobble: a vertical sine that drifts with time,
        # stronger toward the top (s - Yp) so the flame tips bend in the wind.
        wobble = np.sin(((Yp - 0.5) / s + t2 + _wave(t3)) * PI2) * 0.15 * (s - Yp)
        xw = Xp + wobble

        v = _perlin_turbulence(xw, Yp / 2.0 + noise_y_time, noise_time, 2.0, 0.5, 3) * 2.0
        # Horizontal window (bright center, dark edges) + taper to the base.
        v *= _triangle(0.5 + xw / s)
        v = np.maximum(0.0, v)
        v *= Yp / s
        v = np.minimum(v, 1.0)  # keep the palette from wrapping

        # paint(v, v*v): palette color at position v, brightness v*v.
        r = np.interp(v, _PAL_POS, _PAL_R)
        g = np.interp(v, _PAL_POS, _PAL_G)
        b = np.interp(v, _PAL_POS, _PAL_B)
        bri = (v * v) * self._max_brightness
        rgb = np.stack([r, g, b], axis=-1) * bri[..., None]
        self._frame = np.clip(rgb * 255.0, 0, 255).astype(np.uint8)  # (h, w, 3)

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
