"""Regression tests for MatrixDisplay's text-scrim compositor geometry.

The scrim buffer must be sized from the CANVAS (`canvas.width` /
`canvas.height` — the logical size the pixel mapper exposes and the size
every effect/scroller renders against), NOT `MatrixDisplay.self.width` /
`self.height`, which are mis-derived for non-U-mapper configs: the
V-mapper 64x64 stack reports `self` = 128x32. Sizing the buffer to
`self.*` made it only 32 rows tall, so the effect's lower half was
dropped and `SetImage` left everything below the text blacked out.

`rgb_matrix_display` imports `rgbmatrix` lazily (only inside
`MatrixDisplay.__init__`), so the module imports fine on the host; we
build a bare instance via `__new__` and drive `_render_with_scrim`
directly with a stub canvas. numpy + Pillow are declared deps
(requirements-flask / requirements-pi).
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "heart-matrix-controller"))

import rgb_matrix_display as rmd  # noqa: E402


class _Canvas64:
    """Stub of the mapper-exposed 64x64 logical canvas; captures SetImage."""

    width = 64
    height = 64

    def __init__(self):
        self.captured = None

    def SetImage(self, image, *args, **kwargs):
        self.captured = np.asarray(image.convert("RGB"))


class _FullFrameEffect:
    """Fills every row 0..63 — incl. the lower half that used to drop."""

    def render(self, canvas):
        for y in range(canvas.height):
            for x in range(canvas.width):
                canvas.SetPixel(x, y, 150, 160, 170)


class _Scroller:
    scrim = 0.6

    def scrim_rects(self):
        # Hugs the text: x in [5, 40), y in [24, 43).
        return [(5, 24, 40, 43)]


def _display_with_wrong_self_dims():
    """A MatrixDisplay whose self.* dims are the V-mapper mis-derivation."""
    d = rmd.MatrixDisplay.__new__(rmd.MatrixDisplay)
    d.width, d.height = 128, 32  # wrong on purpose (should be 64x64)
    d._scrim_buf = None
    return d


def test_scrim_buffer_follows_canvas_not_self_dims():
    d = _display_with_wrong_self_dims()
    c = _Canvas64()
    d._render_with_scrim(c, _FullFrameEffect(), _Scroller(), [(5, 24, 40, 43)])
    assert c.captured.shape == (64, 64, 3)
    assert d._scrim_buf.shape == (64, 64, 3)


def test_scrim_does_not_black_out_below_text():
    d = _display_with_wrong_self_dims()
    c = _Canvas64()
    d._render_with_scrim(c, _FullFrameEffect(), _Scroller(), [(5, 24, 40, 43)])
    img = c.captured
    # Rows above and below the text band retain the effect, not black.
    assert tuple(img[0, 10]) == (150, 160, 170)
    assert tuple(img[60, 10]) == (150, 160, 170)


def test_scrim_dims_only_the_text_rect():
    d = _display_with_wrong_self_dims()
    c = _Canvas64()
    d._render_with_scrim(c, _FullFrameEffect(), _Scroller(), [(5, 24, 40, 43)])
    img = c.captured  # numpy indexing is [y, x]
    # Inside the rect (y=30, x=10): dimmed by (1 - 0.6) → 150*.4, 160*.4, 170*.4.
    assert tuple(img[30, 10]) == (60, 64, 68)
    # Same row but outside the rect's x-extent (x=50): NOT dimmed.
    assert tuple(img[30, 50]) == (150, 160, 170)

def test_scrim_rounds_corners():
    """The four corner pixels of the rect stay undimmed (rounded look);
    edge and interior pixels are dimmed."""
    d = _display_with_wrong_self_dims()
    c = _Canvas64()
    d._render_with_scrim(c, _FullFrameEffect(), _Scroller(), [(5, 24, 40, 43)])
    img = c.captured  # rect x[5,40) y[24,43): corners (y,x) 24/5, 24/39, 42/5, 42/39
    assert tuple(img[24, 5]) == (150, 160, 170)   # top-left corner undimmed
    assert tuple(img[42, 39]) == (150, 160, 170)  # bottom-right corner undimmed
    assert tuple(img[24, 20]) == (60, 64, 68)     # top edge (non-corner) dimmed
    assert tuple(img[30, 10]) == (60, 64, 68)     # interior dimmed
