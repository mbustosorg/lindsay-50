"""Type stub for the `rgbmatrix.graphics` subpackage.

Used by `heart-matrix-controller/scroller.py` for BDF-font text rendering
on the LED panel. The subpackage is a C extension in the real library —
this stub gives Pylance a typed view on macOS where the extension can't
build.

Cross-module types (Canvas, Color) are referenced as `Any` to keep the
stub independent — `__init__.pyi` is the canonical home for `Canvas`,
and importing it from here would create a stub-import dependency.
"""

from typing import Any

class Color:
    """24-bit RGB color object passed to `DrawText`."""

    def __init__(self, r: int, g: int, b: int) -> None: ...

class Font:
    """BDF font handle. Load a font from disk with `LoadFont(path)`."""

    height: int
    baseline: int
    def __init__(self) -> None: ...
    def LoadFont(self, path: str) -> None: ...
    def CharacterWidth(self, codepoint: int) -> int: ...

def DrawText(
    canvas: Any,
    font: Font,
    x: int,
    y: int,
    color: Color,
    text: str,
) -> int: ...
