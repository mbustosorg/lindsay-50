"""Browser-side display for the sign preview.

The existing effect code from lib_shared.patterns/ expects a canvas with the
rgbmatrix API: `canvas.SetPixel(x, y, r, g, b)` for indexed-palette effects
and `canvas.SetImage(pil_image, x, y)` for full-color effects (honeycomb,
video_display). The WebCanvas here is a thin Python class that backs those
calls with a Pillow Image, so the unmodified effect modules can run inside
PyScript.

The browser's main loop converts `canvas.to_imagedata()` into a JS ImageData
and blits it to the HTML5 canvas once per frame. Doing the per-pixel buffer
work in Python and blitting once at the end is simpler (and just as fast)
as a per-call JS bridge.

`WebDisplay` is a `DisplayBase` subclass: it owns the WebCanvas and implements
`render(effect, scroller)` as the same clear → effect.render → scroller.render
sequence the Pi uses, minus the `SwapOnVSync` step (the browser's rAF loop
paces itself).
"""

import logging

from PIL import Image

from lib_shared.display_base import DisplayBase

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
        EffectsCoordinator's `display.render` between frames.
        """
        self.image = Image.new("RGB", (self.width, self.height))


class WebDisplay(DisplayBase):
    """Browser-side DisplayBase subclass.

    The Pi's `MatrixDisplay` exposes a `canvas` attribute (an rgbmatrix Canvas)
    and a `width`/`height`. The patterns access `self.display.canvas.width`
    in their `tick()` methods, so the WebDisplay must have a `.canvas` that
    exposes those. `render(effect, scroller)` composites one frame: clear the
    canvas, draw the effect, draw the scroller. No `SwapOnVSync` — the
    browser's rAF loop in static/preview.js handles pacing.
    """

    # Narrow the parent's `canvas: object` declaration to the concrete
    # WebCanvas type so Pylance/pyright see `clear()` / `SetPixel()` etc.
    # as known attributes on `self.canvas` (otherwise Pylance infers
    # `object` from the untyped `__init__` parameter).
    canvas: WebCanvas

    def __init__(self, canvas):
        self.canvas = canvas
        self.width = canvas.width
        self.height = canvas.height

    def clear(self):
        self.canvas.clear()

    def render(self, effect, scroller):
        self.canvas.clear()
        effect.render(self.canvas)
        scroller.render(self.canvas)
