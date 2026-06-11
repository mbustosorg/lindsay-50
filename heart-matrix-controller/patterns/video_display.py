"""Video pattern: plays a looping video on the panel.

Unlike the other patterns this does NOT go through the indexed Bitmap/Palette
pipeline — per-frame quantize + per-pixel blit would be far too slow for video.
Instead it decodes frames with OpenCV, paces them at the source fps, and blits
each whole frame with the matrix's C-level canvas.SetImage().

Config:
    VIDEO_PATH  path to a video file (default: first file in design/videos)
    VIDEO_FPS   override the source frame rate (optional)
"""

import logging
import time
from pathlib import Path

from rgb_display import Effect
from lib_shared.config_reader import get_config

logger = logging.getLogger("heart")

_VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".webm", ".gif")


class VideoDisplay(Effect):
    """Looping video, blitted whole-frame via canvas.SetImage()."""

    def __init__(self, display, path=None, fps=None):
        cfg = get_config()
        self._w = display.canvas.width
        self._h = display.canvas.height
        self._force_fps = float(cfg.if_exists("VIDEO_FPS") or 0) or fps

        self._brightness = 1.0
        self._frame = None          # current panel-sized PIL RGB image
        self._cap = None
        self._interval = 1.0 / 30.0
        self._last = time.monotonic()

        if path is None:
            path = cfg.if_exists("VIDEO_PATH") or self._find_default()
        self._path = path
        self._open()

    def _find_default(self):
        # patterns/ -> heart-matrix-controller/ -> repo root -> design/videos
        vdir = Path(__file__).resolve().parent.parent.parent / "design" / "videos"
        vids = sorted(p for p in vdir.glob("*") if p.suffix.lower() in _VIDEO_EXTS)
        return str(vids[0]) if vids else None

    def _open(self):
        if not self._path or not Path(self._path).exists():
            logger.warning("VideoDisplay: no video found (VIDEO_PATH=%s)", self._path)
            return
        try:
            import cv2
        except ImportError:
            logger.warning("VideoDisplay: opencv not installed; video disabled")
            return
        self._cap = cv2.VideoCapture(str(self._path))
        src_fps = self._force_fps or self._cap.get(cv2.CAP_PROP_FPS) or 30.0
        self._interval = 1.0 / max(src_fps, 1.0)
        logger.info("VideoDisplay: %s @ %.1f fps", self._path, 1.0 / self._interval)
        self._last = time.monotonic()
        self._next_frame()

    def _next_frame(self):
        import cv2
        from PIL import Image

        ok, frame = self._cap.read()
        if not ok:                                   # end of file -> loop
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self._cap.read()
            if not ok:
                return
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        img.thumbnail((self._w, self._h), Image.LANCZOS)  # fit, keep aspect
        canvas_img = Image.new("RGB", (self._w, self._h), (0, 0, 0))
        canvas_img.paste(img, ((self._w - img.width) // 2, (self._h - img.height) // 2))
        self._frame = canvas_img

    # -- Effect interface (overridden; no Bitmap/Palette needed) --

    def set_brightness(self, b):
        self._brightness = b

    def tick(self):
        if self._cap is None:
            return
        now = time.monotonic()
        if now - self._last >= self._interval:
            self._last = now
            self._next_frame()

    def render(self, canvas):
        img = self._frame
        if img is None:
            return
        if self._brightness < 1.0:
            img = img.point(lambda v: int(v * self._brightness))  # C-level dim
        canvas.SetImage(img)