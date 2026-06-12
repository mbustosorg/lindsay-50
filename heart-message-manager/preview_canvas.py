"""Browser-side canvas shim for the sign preview.

The existing effect code from heart-matrix-controller/ expects a canvas with
the rgbmatrix API: `canvas.SetPixel(x, y, r, g, b)` for indexed-palette
effects and `canvas.SetImage(pil_image, x, y)` for full-color effects
(honeycomb, video_display). The WebCanvas here is a thin Python class that
backs those calls with a Pillow Image, so the unmodified effect modules can
run inside PyScript.

The browser's main loop converts `canvas.to_imagedata()` into a JS ImageData
and blits it to the HTML5 canvas once per frame. Doing the per-pixel buffer
work in Python and blitting once at the end is simpler (and just as fast)
as a per-call JS bridge.

The WebDisplay wrapper is just enough so the patterns' `display.canvas.width`
and `display.canvas.height` lookups resolve.
"""

import logging

from PIL import Image

log = logging.getLogger("heart")


class WebCanvas:
    """Pillow-backed canvas exposing the rgbmatrix Canvas subset the
    effects use. Lives in the browser; blits to <canvas> once per frame.

    Attributes:
        width, height: panel dimensions in pixels (set at construction).
        image: the Pillow Image frame buffer the effects paint into. The
               browser reads this once per frame and blits it to the
               HTML5 canvas via `to_imagedata`.
    """

    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.image = Image.new("RGB", (width, height))

    def SetPixel(self, x, y, r, g, b):
        """Set one pixel to (r, g, b). Out-of-bounds writes are silently dropped."""
        if 0 <= x < self.width and 0 <= y < self.height:
            self.image.putpixel((x, y), (r, g, b))

    def SetImage(self, pil_image, x=0, y=0):
        """Paste a full-color PIL image into the frame buffer at (x, y).

        Matches the rgbmatrix canvas.SetImage signature the device's
        full-color effects (honeycomb, video_display) rely on.
        """
        self.image.paste(pil_image, (x, y))

    def to_imagedata(self):
        """Convert the frame buffer to a JS ImageData-compatible object.

        In the browser (Pyodide), this is called by the JS main loop and
        returns a Pyodide-converted Uint8ClampedArray of the RGBA bytes.
        The standard CPython environment doesn't have pyodide.ffi.to_js,
        so the fallback here returns the raw bytes — useful for tests
        and the off-line size check.
        """
        rgba = self.image.convert("RGBA")
        raw_bytes = rgba.tobytes()
        try:
            from pyodide.ffi import to_js  # type: ignore

            return to_js(raw_bytes)
        except ImportError:
            return raw_bytes

    def clear(self):
        """Reset the frame buffer to black (RGB 0,0,0).

        The device's display.Clear() is called per frame; the preview's
        effect render() typically overwrites every pixel it cares about
        (index 0 in the palette is the "background" skip), so an explicit
        clear is not always required — but it is used by the
        PreviewCoordinator between fades.
        """
        self.image = Image.new("RGB", (self.width, self.height))


class WebDisplay:
    """Adapter so the patterns' `display.canvas.width/height` lookups work.

    The Pi's `Display` exposes a `canvas` attribute (an rgbmatrix Canvas)
    and a `width`/`height`. The patterns access `self.display.canvas.width`
    in their `tick()` methods, so the WebDisplay must have a `.canvas` that
    exposes those.
    """

    def __init__(self, canvas):
        self.canvas = canvas
        self.width = canvas.width
        self.height = canvas.height
