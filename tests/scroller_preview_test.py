"""Tests for heart-message-manager.preview_scroller.PreviewScroller.

The browser-side scroller subclasses ScrollerBase and uses Pillow for
font metrics + glyph blits. We stub the WebCanvas so we don't need
PyScript; the underlying Pillow Image is what the scroller actually
mutates.
"""

import time
from unittest.mock import MagicMock

import pytest
from PIL import Image

# Ensure project root is on the path so lib_shared is importable
import sys
import importlib.util
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


def _load_preview_scroller():
    """Load heart-message-manager/preview_scroller.py via importlib (hyphen-safe)."""
    scroller_path = _PROJECT_ROOT / "heart-message-manager" / "preview_scroller.py"
    spec = importlib.util.spec_from_file_location("heart_message_manager.preview_scroller", str(scroller_path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["heart_message_manager.preview_scroller"] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_canvas_module():
    """Load preview_canvas.py via importlib."""
    canvas_path = _PROJECT_ROOT / "heart-message-manager" / "preview_canvas.py"
    spec = importlib.util.spec_from_file_location("heart_message_manager.preview_canvas", str(canvas_path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["heart_message_manager.preview_canvas"] = mod
    spec.loader.exec_module(mod)
    return mod


class _StubDisplay:
    class _Canvas:
        width = 64
        height = 64

    canvas = _Canvas()
    width = 64
    height = 64


def _stub_canvas():
    """Return a stub object with .image pointing at a real Pillow Image.

    Matches the WebCanvas interface the PreviewScroller actually needs:
    an `image` attribute that holds the frame buffer.
    """
    canvas = MagicMock()
    canvas.image = Image.new("RGB", (64, 64))
    return canvas


def test_preview_scroller_inherits_scroller_base():
    """PreviewScroller is a ScrollerBase subclass — the shared math applies."""
    from lib_shared.scroller_base import ScrollerBase

    mod = _load_preview_scroller()
    assert issubclass(mod.PreviewScroller, ScrollerBase)


def test_preview_scroller_init_computes_single_line_layout():
    """A 64x64 canvas uses single-line layout (top_y == bottom_y).

    The browser preview matches the device's MatrixScroller: a single
    orange line centered on the full display (see PreviewScroller.
    compute_layout — "Place the baseline for a single line centered on
    the full display."). 64x64 panels do not stack two lines.
    """
    mod = _load_preview_scroller()
    s = mod.PreviewScroller(_StubDisplay())
    assert s.single_line is True
    assert s.top_y == s.bottom_y


def test_preview_scroller_set_text_initializes_positions():
    """set_text with a width populates text, text_width, and x positions."""
    mod = _load_preview_scroller()
    s = mod.PreviewScroller(_StubDisplay())
    s.set_text("hello", canvas_width=64)
    assert s.text == "hello"
    assert s.text_width > 0  # Pillow default font returns a positive width
    assert s.top_x == 64
    assert s.bottom_x == 64


def test_preview_scroller_tick_advances_x_by_expected_pixels(monkeypatch):
    """0.5s @ frame_delay=0.05 -> 10 pixels of motion, matching the base class."""
    mod = _load_preview_scroller()
    state = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: state["t"])

    s = mod.PreviewScroller(_StubDisplay(), frame_delay=0.05)
    s.set_text("hi", canvas_width=64)
    initial_top = s.top_x

    state["t"] += 0.5
    s.tick(canvas_width=64)
    assert s.top_x == initial_top - 10


def test_preview_scroller_baselines_match_single_line_panel_center():
    """For a 64-tall canvas, the single line is centered on the full display.

    The exact pixel offset depends on the default font's metrics, but
    the line must be in the middle band of the panel — close to height/2.
    """
    mod = _load_preview_scroller()
    s = mod.PreviewScroller(_StubDisplay())
    # Single-line: top_y == bottom_y. Centered near height/2 (32). The
    # bundled Pillow default font is ~11px tall, so top_y is roughly
    # (64 - 11) // 2 = 26; the test asserts a 16..48 band to be robust
    # to font-metric changes.
    assert s.top_y == s.bottom_y
    assert 16 <= s.top_y <= 48


def test_preview_scroller_measure_text_uses_pillow_bbox():
    """measure_text returns bbox-derived pixel width (positive for non-empty)."""
    mod = _load_preview_scroller()
    s = mod.PreviewScroller(_StubDisplay())
    w_short = s.measure_text("hi")
    w_long = s.measure_text("hello world")
    assert w_short > 0
    assert w_long > w_short


def test_preview_scroller_draw_text_writes_to_canvas_image():
    """draw_text uses ImageDraw to paint glyphs into the canvas's .image."""
    mod = _load_preview_scroller()
    s = mod.PreviewScroller(_StubDisplay(), color=0xFF0000)
    canvas = _stub_canvas()
    s.draw_text(canvas, "hi", x=0, y=0, color=(255, 0, 0))
    # The canvas image was modified — at least one pixel in the top-left
    # quadrant should be non-black now.
    pixels = list(canvas.image.getdata())
    non_black = [p for p in pixels if p != (0, 0, 0)]
    assert len(non_black) > 0


def test_preview_scroller_single_line_layout_for_short_canvas():
    """A 64x16 canvas (height <= 32) uses single-line mode."""

    class _ShortDisplay:
        class _Canvas:
            width = 64
            height = 16

        canvas = _Canvas()
        width = 64
        height = 16

    mod = _load_preview_scroller()
    s = mod.PreviewScroller(_ShortDisplay())
    assert s.single_line is True
