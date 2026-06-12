"""Tests for heart-message-manager.preview_canvas.WebCanvas and WebDisplay."""

import importlib.util
import sys
from pathlib import Path

import pytest
from PIL import Image

# Ensure project root is on the path so lib_shared is importable
_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _restore_lib_shared():
    """test_auth.py replaces sys.modules['lib_shared'] with a Mock. Re-import
    the real package before each test.
    """
    for mod_name in list(sys.modules):
        if mod_name == "lib_shared" or mod_name.startswith("lib_shared."):
            del sys.modules[mod_name]
    importlib.import_module("lib_shared")
    importlib.import_module("lib_shared.config_reader")
    yield


def _load_canvas_module():
    canvas_path = _PROJECT_ROOT / "heart-message-manager" / "preview_canvas.py"
    spec = importlib.util.spec_from_file_location(
        "heart_message_manager.preview_canvas", str(canvas_path)
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["heart_message_manager.preview_canvas"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_webcanvas_init_creates_black_rgb_image():
    """WebCanvas(64, 64) starts as a 64x64 RGB image filled with black."""
    mod = _load_canvas_module()
    c = mod.WebCanvas(64, 64)
    assert c.width == 64
    assert c.height == 64
    assert c.image.mode == "RGB"
    assert c.image.size == (64, 64)
    # All pixels are black on init
    assert c.image.getpixel((0, 0)) == (0, 0, 0)
    assert c.image.getpixel((63, 63)) == (0, 0, 0)


def test_webcanvas_setpixel_writes_expected_rgb():
    """SetPixel(x, y, r, g, b) sets the corresponding pixel to that color."""
    mod = _load_canvas_module()
    c = mod.WebCanvas(64, 64)
    c.SetPixel(10, 20, 255, 128, 64)
    assert c.image.getpixel((10, 20)) == (255, 128, 64)
    # Other pixels are still black
    assert c.image.getpixel((0, 0)) == (0, 0, 0)
    assert c.image.getpixel((11, 20)) == (0, 0, 0)


def test_webcanvas_setpixel_out_of_bounds_silently_drops():
    """SetPixel with negative or out-of-range coords doesn't raise."""
    mod = _load_canvas_module()
    c = mod.WebCanvas(64, 64)
    # Should not raise
    c.SetPixel(-1, 0, 255, 0, 0)
    c.SetPixel(0, -1, 255, 0, 0)
    c.SetPixel(64, 0, 255, 0, 0)
    c.SetPixel(0, 64, 255, 0, 0)
    # All pixels still black
    assert c.image.getpixel((0, 0)) == (0, 0, 0)


def test_webcanvas_setimage_pastes_at_origin():
    """SetImage(pil_image, x, y) pastes the source image at the given offset."""
    mod = _load_canvas_module()
    c = mod.WebCanvas(64, 64)
    src = Image.new("RGB", (8, 8), (10, 20, 30))
    c.SetImage(src, 5, 5)
    # Pixel at (5, 5) is the start of the pasted image
    assert c.image.getpixel((5, 5)) == (10, 20, 30)
    # Pixel at (12, 12) is still inside the pasted image
    assert c.image.getpixel((12, 12)) == (10, 20, 30)
    # Pixel outside the paste region remains black
    assert c.image.getpixel((4, 5)) == (0, 0, 0)


def test_webcanvas_setimage_default_origin_is_zero():
    """SetImage(pil_image) with no x/y defaults to (0, 0)."""
    mod = _load_canvas_module()
    c = mod.WebCanvas(64, 64)
    src = Image.new("RGB", (4, 4), (200, 100, 50))
    c.SetImage(src)
    assert c.image.getpixel((2, 2)) == (200, 100, 50)


def test_webcanvas_to_imagedata_returns_rgba_bytes_of_correct_size():
    """to_imagedata() returns 64*64*4 = 16384 bytes (RGBA per pixel)."""
    mod = _load_canvas_module()
    c = mod.WebCanvas(64, 64)
    result = c.to_imagedata()
    # In CPython, pyodide.ffi.to_js is not available, so the fallback returns raw bytes
    assert isinstance(result, (bytes, bytearray))
    assert len(result) == 64 * 64 * 4


def test_webcanvas_clear_resets_to_black():
    """clear() returns the frame buffer to all-black."""
    mod = _load_canvas_module()
    c = mod.WebCanvas(64, 64)
    c.SetPixel(10, 10, 255, 0, 0)
    assert c.image.getpixel((10, 10)) == (255, 0, 0)
    c.clear()
    assert c.image.getpixel((10, 10)) == (0, 0, 0)
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
