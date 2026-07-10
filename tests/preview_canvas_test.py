"""Tests for heart-message-manager.preview_display.WebCanvas and WebDisplay."""

import importlib.util
import sys
from pathlib import Path

import pytest
from PIL import Image

# Ensure project root is on the path so lib_shared is importable
_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


def _load_canvas_module():
    canvas_path = _PROJECT_ROOT / "heart-message-manager" / "preview_display.py"
    spec = importlib.util.spec_from_file_location("heart_message_manager.preview_display", str(canvas_path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["heart_message_manager.preview_display"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_webcanvas_init_creates_transparent_rgba_image():
    """WebCanvas(64, 64) starts as a 64x64 RGBA image fully transparent (alpha=0).

    The default state is transparent so pixels the effect doesn't paint
    (e.g. palette index 0 skipped by `Effect.render`) show whatever's
    behind the canvas in the DOM — the BrowserMediaOverlay's
    `<img>` / `<video>` element when media is active, the parent
    div's `bg-slate-900` when it isn't.
    """
    mod = _load_canvas_module()
    c = mod.WebCanvas(64, 64)
    assert c.width == 64
    assert c.height == 64
    assert c.image.mode == "RGBA"
    assert c.image.size == (64, 64)
    # All pixels are transparent on init: (R, G, B, A) = (0, 0, 0, 0)
    assert c.image.getpixel((0, 0)) == (0, 0, 0, 0)
    assert c.image.getpixel((63, 63)) == (0, 0, 0, 0)


def test_webcanvas_setpixel_writes_expected_rgba_opaque():
    """SetPixel(x, y, r, g, b) sets the pixel to (r, g, b) at full alpha (255).

    Lit pixels must be opaque (alpha=255) so the browser composites
    them over the background DOM layer correctly; only gaps stay
    transparent.
    """
    mod = _load_canvas_module()
    c = mod.WebCanvas(64, 64)
    c.SetPixel(10, 20, 255, 128, 64)
    assert c.image.getpixel((10, 20)) == (255, 128, 64, 255)
    # Other pixels stay transparent
    assert c.image.getpixel((0, 0)) == (0, 0, 0, 0)
    assert c.image.getpixel((11, 20)) == (0, 0, 0, 0)


def test_webcanvas_setpixel_out_of_bounds_silently_drops():
    """SetPixel with negative or out-of-range coords doesn't raise."""
    mod = _load_canvas_module()
    c = mod.WebCanvas(64, 64)
    # Should not raise
    c.SetPixel(-1, 0, 255, 0, 0)
    c.SetPixel(0, -1, 255, 0, 0)
    c.SetPixel(64, 0, 255, 0, 0)
    c.SetPixel(0, 64, 255, 0, 0)
    # All pixels still transparent
    assert c.image.getpixel((0, 0)) == (0, 0, 0, 0)


def test_webcanvas_setimage_pastes_at_origin():
    """SetImage(pil_image, x, y) pastes an RGB source image at the given offset.

    RGB sources have no alpha — `SetImage` pastes the RGB bytes and
    the destination pixels land opaque (the destination's existing
    alpha would otherwise be preserved, defeating the "lit pixels are
    opaque" invariant the canvas layering relies on).
    """
    mod = _load_canvas_module()
    c = mod.WebCanvas(64, 64)
    src = Image.new("RGB", (8, 8), (10, 20, 30))
    c.SetImage(src, 5, 5)
    # Pixel at (5, 5) is the start of the pasted image, now opaque
    assert c.image.getpixel((5, 5)) == (10, 20, 30, 255)
    # Pixel at (12, 12) is still inside the pasted image
    assert c.image.getpixel((12, 12)) == (10, 20, 30, 255)
    # Pixel outside the paste region remains transparent
    assert c.image.getpixel((4, 5)) == (0, 0, 0, 0)


def test_webcanvas_setimage_default_origin_is_zero():
    """SetImage(pil_image) with no x/y defaults to (0, 0)."""
    mod = _load_canvas_module()
    c = mod.WebCanvas(64, 64)
    src = Image.new("RGB", (4, 4), (200, 100, 50))
    c.SetImage(src)
    assert c.image.getpixel((2, 2)) == (200, 100, 50, 255)


def test_webcanvas_setimage_lifts_destination_alpha_to_opaque():
    """SetImage always lands alpha=255 on the pasted region regardless of source alpha.

    The canvasing's "lit pixels are opaque, gaps are transparent"
    invariant is what the DOM layering relies on — an RGBA source
    with alpha=128 would otherwise paste at alpha=128 onto the
    canvas, which the parent div's bg-slate-900 would then bleed
    through and tint the effect. SetImage's job is to make sure
    the lit region always fully covers what's behind it.
    """
    mod = _load_canvas_module()
    c = mod.WebCanvas(64, 64)
    src = Image.new("RGBA", (4, 4), (10, 20, 30, 128))
    c.SetImage(src)
    # RGB preserved from source, alpha lifted to 255
    assert c.image.getpixel((2, 2)) == (10, 20, 30, 255)
    # Pixels outside the paste region stay transparent
    assert c.image.getpixel((5, 5)) == (0, 0, 0, 0)


def test_webcanvas_to_imagedata_returns_rgba_bytes_of_correct_size():
    """to_imagedata() returns 64*64*4 = 16384 bytes (RGBA per pixel)."""
    mod = _load_canvas_module()
    c = mod.WebCanvas(64, 64)
    result = c.to_imagedata()
    # In CPython, pyodide.ffi.to_js is not available, so the fallback returns raw bytes
    assert isinstance(result, (bytes, bytearray))
    assert len(result) == 64 * 64 * 4


def test_webcanvas_clear_resets_to_transparent():
    """clear() returns the frame buffer to fully-transparent (alpha=0).

    Lit pixels are opaque (set by SetPixel / SetImage); gaps stay
    transparent (the canvas default). `clear()` resets the buffer to
    that transparent default so the next frame's effects start with
    a clean canvas.
    """
    mod = _load_canvas_module()
    c = mod.WebCanvas(64, 64)
    c.SetPixel(10, 10, 255, 0, 0)
    assert c.image.getpixel((10, 10)) == (255, 0, 0, 255)
    c.clear()
    assert c.image.getpixel((10, 10)) == (0, 0, 0, 0)
    # Size preserved
    assert c.image.size == (64, 64)


def test_webdisplay_exposes_canvas_width_height():
    """WebDisplay(canvas) makes the canvas's dimensions available as
    `display.width` and `display.height`, and the canvas itself at
    `display.canvas` (matching the rgbmatrix Display contract)."""
    mod = _load_canvas_module()
    canvas = mod.WebCanvas(64, 64)
    d = mod.WebDisplay(canvas)
    assert d.canvas is canvas
    assert d.width == 64
    assert d.height == 64
    # Patterns read these on every tick
    assert d.canvas.width == 64
    assert d.canvas.height == 64
