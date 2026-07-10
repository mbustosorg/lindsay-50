"""Video renderer (issue #38 / openspec `mms-media-support`).

Plays a video on the panel via OpenCV's `VideoCapture`, blitting each
decoded frame whole-frame with `canvas.SetImage(...)`. This bypasses
the indexed `Bitmap`/`Palette` pipeline because per-frame quantize
+ per-pixel blit would be too slow for video.

This is an *inner renderer* consumed by `MediaCycler` (per-message
background effect) via direct import. It is NOT an entry in the
effects registry — operators don't see a "VideoDisplay" row in
/settings (same shape as `ImageDisplay`).

Per-item frame loop uses `cv2.VideoCapture.grab()` + `retrieve()` —
NOT `read()` — so per-frame memory is bounded by frame dimensions
(~6 MB for 1080p), not by total video size. A 50 MB MP4 does not
land in Pi RAM.

Construction tolerates `cv2` being absent (e.g., in host-side tests):
the renderer logs a WARNING, sets `_cap = None`, and `tick` /
`render` become no-ops. The outer `MediaCycler` interprets the
no-op pattern as a codec failure on first frame and drops the item
(D12).
"""

import logging
import time
from pathlib import Path
from typing import Any, Optional

from lib_shared.display_base import DisplayBase
from lib_shared.effect_base import Effect

logger = logging.getLogger("heart")


class VideoDisplay(Effect):
    """Looping video, blitted whole-frame via canvas.SetImage().

    Public surface is the Effect interface — `set_brightness`,
    `tick`, `render` — so `MediaCycler` can wrap this class without
    a custom adapter. `cv2` is lazy-imported inside `_open()` so the
    module is importable on hosts without OpenCV (the host test
    suite has no `cv2` in requirements-flask.txt — only the Pi does).
    """

    def __init__(self, display: DisplayBase, path: str | None = None, fps: float | None = None) -> None:
        """Initialize the renderer.

        Args:
            display: DisplayBase whose `canvas.width` / `canvas.height`
                drive the output frame dimensions.
            path: Local filesystem path to a video file. The Pi passes
                a path the MediaCycler cached from the S3 media fetch.
                None is tolerated (logs WARNING, sets `_cap = None`).
            fps: Override source frame rate. None = read from the file
                metadata via `cv2.CAP_PROP_FPS`.
        """
        self._w = display.canvas.width
        self._h = display.canvas.height
        self._force_fps = fps

        self._brightness: float = 1.0
        self._frame: Any = None  # current panel-sized PIL RGB image
        self._cap: Any = None  # cv2.VideoCapture; opencv has no type stubs
        self._interval: float = 1.0 / 30.0
        self._last: float = time.monotonic()
        self._path: Optional[str] = path

        if path is None:
            logger.warning("VideoDisplay: no path provided; video disabled")
            return
        if not Path(path).exists():
            logger.warning("VideoDisplay: path does not exist: %s", path)
            return
        self._open()

    def _open(self) -> None:
        try:
            import cv2  # type: ignore[import-not-found]  # OpenCV ships no type stubs
        except ImportError:
            logger.warning("VideoDisplay: opencv not installed; video disabled")
            return
        self._cap = cv2.VideoCapture(str(self._path))
        if not self._cap.isOpened():
            logger.warning("VideoDisplay: cannot open %s; video disabled", self._path)
            self._cap = None
            return
        src_fps = self._force_fps or self._cap.get(cv2.CAP_PROP_FPS) or 30.0
        self._interval = 1.0 / max(src_fps, 1.0)
        logger.info("VideoDisplay: %s @ %.1f fps", self._path, 1.0 / self._interval)
        self._last = time.monotonic()

    def _next_frame(self) -> bool:
        """Advance to the next frame. Returns True on success.

        Uses `grab()` + `retrieve()` to bound per-frame memory by
        frame dimensions (NOT total video size). Loops on EOF.
        Returns False if the capture is closed or both `grab` and
        EOF-loop `grab` failed.
        """
        if self._cap is None:
            return False
        import cv2  # type: ignore[import-not-found]

        # Try the next frame in the file.
        if self._cap.grab():
            ok, frame = self._cap.retrieve()
            if ok:
                self._set_frame(frame)
                return True
        # EOF — rewind and try once more.
        self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        if self._cap.grab():
            ok, frame = self._cap.retrieve()
            if ok:
                self._set_frame(frame)
                return True
        return False

    def _set_frame(self, frame) -> None:
        from PIL import Image
        from PIL.Image import Resampling

        import cv2  # type: ignore[import-not-found]

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        img.thumbnail((self._w, self._h), Resampling.LANCZOS)  # fit, keep aspect
        canvas_img = Image.new("RGB", (self._w, self._h), (0, 0, 0))
        canvas_img.paste(img, ((self._w - img.width) // 2, (self._h - img.height) // 2))
        self._frame = canvas_img

    # -- Effect interface (overridden; no Bitmap/Palette needed) --

    def set_brightness(self, b: float) -> None:
        self._brightness = b

    def tick(self) -> None:
        if self._cap is None:
            return
        now = time.monotonic()
        if now - self._last >= self._interval:
            self._last = now
            self._next_frame()

    def render(self, canvas) -> None:
        img = self._frame
        if img is None:
            return
        # Apply the brightness scaling when `b != 1.0` (the prior
        # gate was `b < 1.0`, which silently dropped MediaCycler's
        # ~15% brightness boost at full panel brightness — the gate
        # tripped before the scaling could run). The boost pushes
        # dark pixels brighter; saturated pixels are held at 255
        # by the `min(255, …)` clamp inside the point lambda so a
        # `b > 1.0` value never produces an out-of-range channel
        # that PIL would wrap modulo 256.
        if self._brightness != 1.0:
            # `point` is a C-level operation — much cheaper than
            # iterating per-pixel from Python.
            img = img.point(lambda v: min(255, int(v * self._brightness)))
        canvas.SetImage(img)

    @property
    def cap(self):
        """The underlying cv2.VideoCapture (or None when cv2 is missing / open failed)."""
        return self._cap

    def close(self) -> None:
        """Release the OpenCV capture. Idempotent."""
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception as e:  # noqa: BLE001 — release() raises opaque cv2 errors
                logger.debug("VideoDisplay.release: %s", e)
            self._cap = None
