"""Image display renderer (issue #38 / openspec `image-display-pattern`).

Renders still images on the LED matrix through the indexed `Bitmap` /
`Palette` pipeline. Supports the formats Twilio can deliver as MMS
attachments: PNG, JPEG, GIF, WebP.

This is an *inner renderer* consumed by `MediaCycler` (per-message
background effect) via direct import. It is NOT an entry in the
effects registry — operators don't see an "ImageDisplay" row in
/settings. The class exposes the same `set_brightness` / `tick` /
`render` surface as the other effects so `MediaCycler` can wrap it
without a custom adapter.

Per-format load rules:
  * PNG with alpha (`mode == "RGBA"`): apply the alpha channel as an
    ink mask painted white-on-black — the source art is a black
    drawing on a transparent background, and reading the mask gives
    the cleanest fit on the unlit panel.
  * JPEG / GIF / WebP (RGB): drop alpha via `convert("RGB")`, fit to
    the panel, quantize to 256 colors. No mask — the source is
    already a color photo.
  * Anything else: load with `convert("RGB")` as a safe fallback.

Crossfade between images: identical to the pre-rename `PngDisplay`
implementation — `hold -> out -> in -> hold` state machine with a
configurable interval and a gamma-shaped per-image fade level.
"""

import logging
import re
import time
from pathlib import Path

from lib_shared.display_base import DisplayBase
from lib_shared.effect_base import Bitmap, Palette, Effect
from lib_shared.config_reader import get_config

logger = logging.getLogger("heart")


_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def _natural_key(path: Path) -> list:
    """Sort key so 'Artboard 2' precedes 'Artboard 10'."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", path.name)]


class ImageDisplay(Effect):
    """Slideshow of still images (PNG/JPEG/GIF/WebP) on the LED panel.

    Renders through the indexed `Bitmap`/`Palette` pipeline so it
    composes correctly with the crossfade the EffectCoordinator drives
    on every pattern switch (see `set_brightness`).

    The image source is either a directory of images (globbed at
    construction; natural-sorted) or a single path. `MediaCycler`
    uses the single-path mode to display a single S3-fetched
    attachment per cycle slot.
    """

    def __init__(
        self,
        display: DisplayBase,
        path: str | Path | None = None,
        dir: str | Path | None = None,  # noqa: A002 - matches the pre-rename kwarg
        interval: float = 8.0,
        fade: float = 0.6,
        gamma: float = 2.2,
    ) -> None:
        cfg = get_config()
        # Match the sibling effects: source geometry from the mapped canvas.
        self._w = display.canvas.width
        self._h = display.canvas.height
        self._interval = float(cfg.if_exists("PNG_INTERVAL") or interval)
        self._fade = float(cfg.if_exists("PNG_FADE") or fade)  # crossfade seconds
        self._gamma = gamma  # perceptually-linear fade, matches EffectCoordinator

        if path is not None:
            # Single-image mode: MediaCycler hands us one S3-fetched
            # path. Treat as a 1-element list so the existing
            # `hold/out/in` state machine and `render` path Just Work.
            self._paths: list[Path] = [Path(path)]
        elif dir is not None:
            self._paths = sorted(
                (p for p in Path(dir).iterdir() if p.suffix.lower() in _IMAGE_EXTS),
                key=_natural_key,
            )
        else:
            default = cfg.if_exists("PNG_DIR")
            if default:
                self._paths = sorted(
                    (p for p in Path(default).iterdir() if p.suffix.lower() in _IMAGE_EXTS),
                    key=_natural_key,
                )
            else:
                # patterns/ -> heart-matrix-controller/ -> repo root -> design/pngs
                self._paths = sorted(
                    (
                        p
                        for p in (Path(__file__).resolve().parent.parent.parent / "design" / "pngs").iterdir()
                        if p.suffix.lower() in _IMAGE_EXTS
                    ),
                    key=_natural_key,
                )

        self._index = 0
        self._coord_b = 1.0  # brightness from the EffectCoordinator's global fades
        self._img_b = 1.0  # internal per-image crossfade level (0..1)
        self._phase = "hold"  # hold -> out -> (swap) -> in -> hold
        self._phase_start = time.monotonic()

        if self._paths:
            logger.info("ImageDisplay: %d image(s) from %s", len(self._paths), self._paths[0].parent)
            self._load_current()
        else:
            logger.warning("ImageDisplay: no images found (path=%s, dir=%s)", path, dir)
            self.bitmap = Bitmap(self._w, self._h)  # all index 0
            self.palette = Palette(1)  # index 0 -> black
            self._init_render()

    # -- image loading ------------------------------------------------------

    def _load_current(self) -> None:
        self._render_image(self._paths[self._index])
        self._init_render()  # recapture palette for brightness scaling
        self._apply()  # reapply the current combined fade level

    def _render_image(self, path: Path) -> None:
        """Load, fit, and render one image into bitmap/palette.

        PNG with alpha: alpha-as-mask painted white-on-black.
        Everything else: drop alpha, fit to panel, quantize to 256
        colors. The format dispatch is per-image so a mixed-format
        directory just works.
        """
        from PIL import Image  # lazy: only the Pi needs Pillow
        from PIL.Image import Resampling  # explicit: Pylance resolves Resampling.LANCZOS

        w, h = self._w, self._h
        img = Image.open(path)
        if img.mode == "RGBA":
            # PNG with alpha: treat the alpha channel as the ink mask.
            img.thumbnail((w, h), Resampling.LANCZOS)
            mask = img.getchannel("A")
            frame = Image.new("RGB", (w, h), (0, 0, 0))
            white = Image.new("RGB", img.size, (255, 255, 255))
            offset = ((w - img.width) // 2, (h - img.height) // 2)
            frame.paste(white, offset, mask)  # white where drawn, black elsewhere
        else:
            # JPEG / GIF / WebP / anything else: drop alpha via RGB.
            img = img.convert("RGB")
            img.thumbnail((w, h), Resampling.LANCZOS)
            frame = Image.new("RGB", (w, h), (0, 0, 0))
            offset = ((w - img.width) // 2, (h - img.height) // 2)
            frame.paste(img, offset)

        quant = frame.quantize(colors=256)
        pal = quant.getpalette() or []
        palette = Palette(256)
        for i in range(len(pal) // 3):
            r, g, b = pal[i * 3 : i * 3 + 3]
            palette[i] = (r << 16) | (g << 8) | b

        bitmap = Bitmap(w, h)
        bitmap._buf[:] = quant.tobytes()  # one palette index per pixel, row-major
        self.bitmap = bitmap
        self.palette = palette

    # -- Effect interface ---------------------------------------------------

    def set_brightness(self, b: float) -> None:
        # Called by the EffectCoordinator for global pattern-switch fades.
        self._coord_b = b
        if b == 0.0:
            # We're (de)activating — restart the slideshow from a full
            # image so the coordinator's fade-in shows a complete
            # frame, not a mid-crossfade.
            self._img_b = 1.0
            self._phase = "hold"
            self._phase_start = time.monotonic()
        self._apply()

    def _apply(self) -> None:
        """Drive the palette from the coordinator fade * the per-image fade."""
        super().set_brightness(self._coord_b * self._img_b)

    def tick(self) -> None:
        # Slideshow with a crossfade: hold the image, fade it out, swap, fade in.
        if len(self._paths) <= 1:
            return
        elapsed = time.monotonic() - self._phase_start

        if self._phase == "hold":
            if elapsed >= self._interval:
                self._phase = "out"
                self._phase_start = time.monotonic()
            return

        t = elapsed / self._fade if self._fade > 0 else 1.0
        if self._phase == "out":
            if t >= 1.0:
                self._index = (self._index + 1) % len(self._paths)
                self._img_b = 0.0
                self._load_current()  # new image, still dark
                self._phase = "in"
                self._phase_start = time.monotonic()
                return
            self._img_b = (1.0 - t) ** self._gamma
        else:  # "in"
            if t >= 1.0:
                self._img_b = 1.0
                self._phase = "hold"
                self._phase_start = time.monotonic()
            else:
                self._img_b = t**self._gamma
        self._apply()

    def render(self, canvas) -> None:
        """Draw every pixel (a photo fills the panel — no transparent index 0)."""
        colors = self.palette._colors
        buf = self.bitmap._buf
        w, h = self.bitmap.width, self.bitmap.height
        for y in range(h):
            row = y * w
            for x in range(w):
                c = colors[buf[row + x]]
                canvas.SetPixel(x, y, (c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF)
