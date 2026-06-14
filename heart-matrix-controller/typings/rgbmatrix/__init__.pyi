"""Type stub for the `rgbmatrix` module (hzeller rpi-rgb-led-matrix C extension).

The real package is a C extension that builds only on the Raspberry Pi. On
macOS dev machines it can't be installed, so Pylance resolves the lazy
`from rgbmatrix import ...` import at `rgb_matrix_display.py:33` and
`scroller.py:14` as `Any` — and downstream `Clear`/`SetPixel`/etc. attribute
access on the un-typed canvas object trips `reportAttributeAccessIssue`.

This stub gives Pylance a typed view of the symbols the project actually
uses. It is invisible to Python at runtime (Pylance/Pyright read it; the
interpreter does not).

Scope: 100 lines or fewer. Covers exactly the surface used in this repo:
- `RGBMatrix` / `RGBMatrixOptions` for `MatrixDisplay.__init__`
- `Canvas` for `MatrixDisplay.canvas`, the patterns, and the scroller
"""

from typing import Any

class Canvas:
    """Frame canvas owned by `RGBMatrix.CreateFrameCanvas()`."""

    width: int
    height: int
    def Clear(self) -> None: ...
    def SetPixel(self, x: int, y: int, r: int, g: int, b: int) -> None: ...
    def SetImage(self, image: Any) -> None: ...

class RGBMatrixOptions:
    """Hardware configuration for the LED panel chain.

    Attributes are read by the hzeller library at `RGBMatrix(options=...)`
    time. Typed as primitives — the real options object accepts more keys
    (see hzeller docs) but this project only touches these nine.
    """

    rows: int
    cols: int
    chain_length: int
    parallel: int
    hardware_mapping: str
    pixel_mapper_config: str
    pwm_bits: int
    brightness: int
    gpio_slowdown: int
    drop_privileges: bool
    disable_hardware_pulsing: bool

class RGBMatrix:
    """The hzeller rpi-rgb-led-matrix handle.

    Constructed with `RGBMatrix(options=RGBMatrixOptions(...))`. Owns a
    double-buffered `Canvas` returned by `CreateFrameCanvas()`.
    """

    def __init__(self, options: RGBMatrixOptions) -> None: ...
    def CreateFrameCanvas(self) -> Canvas: ...
    def Clear(self) -> None: ...
    def SwapOnVSync(self, canvas: Canvas) -> Canvas: ...
