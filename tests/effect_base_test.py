"""Tests for lib_shared.effect_base primitives.

Covers the small displayio / bitmaptools subset the patterns use:
Bitmap, Palette, arrayblit, and the Effect base class.
"""

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


def _load_effect_base():
    """Import lib_shared.effect_base via importlib so rgbmatrix isn't required."""
    path = _PROJECT_ROOT / "lib_shared" / "effect_base.py"
    spec = importlib.util.spec_from_file_location("lib_shared.effect_base", str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lib_shared.effect_base"] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_lib_shared():
    """Re-import the real lib_shared package (test_auth.py replaces it with a Mock)."""
    for name in [k for k in list(sys.modules) if k == "lib_shared" or k.startswith("lib_shared.")]:
        del sys.modules[name]
    importlib.import_module("lib_shared")


@pytest.fixture(autouse=True)
def _restore_lib_shared():
    _load_lib_shared()
    yield


def test_import_does_not_pull_in_rgbmatrix():
    """lib_shared.effect_base must not import rgbmatrix (the patterns use it)."""
    import ast

    tree = ast.parse((_PROJECT_ROOT / "lib_shared" / "effect_base.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "rgbmatrix" not in alias.name, (
                    f"lib_shared.effect_base must not import {alias.name}; "
                    "rgbmatrix is a native module not available in the browser"
                )
        elif isinstance(node, ast.ImportFrom):
            assert node.module is None or "rgbmatrix" not in node.module, (
                f"lib_shared.effect_base must not import from {node.module}; "
                "rgbmatrix is a native module not available in the browser"
            )


# --- Bitmap ------------------------------------------------------------------


def test_bitmap_set_get():
    mod = _load_effect_base()
    b = mod.Bitmap(4, 3)
    b[1, 2] = 7
    assert b[1, 2] == 7
    assert b[0, 0] == 0


def test_bitmap_fill_zeros():
    mod = _load_effect_base()
    b = mod.Bitmap(4, 3)
    b[2, 1] = 5
    b.fill(0)
    assert b[2, 1] == 0
    assert all(b[x, y] == 0 for x in range(4) for y in range(3))


def test_bitmap_fill_nonzero():
    mod = _load_effect_base()
    b = mod.Bitmap(4, 3)
    b.fill(9)
    assert b[3, 2] == 9
    assert b[0, 0] == 9


# --- Palette -----------------------------------------------------------------


def test_palette_set_get_len():
    mod = _load_effect_base()
    p = mod.Palette(4)
    assert len(p) == 4
    p[1] = 0xFF00AA
    assert p[1] == 0xFF00AA
    assert p[0] == 0


# --- arrayblit ---------------------------------------------------------------


def test_arrayblit_happy_path():
    mod = _load_effect_base()
    b = mod.Bitmap(2, 2)
    buf = bytes([1, 2, 3, 4])
    mod.arrayblit(b, buf)
    assert b[0, 0] == 1
    assert b[1, 0] == 2
    assert b[0, 1] == 3
    assert b[1, 1] == 4


def test_arrayblit_wrong_size_raises():
    mod = _load_effect_base()
    b = mod.Bitmap(2, 2)
    with pytest.raises(ValueError):
        mod.arrayblit(b, bytes([1, 2, 3]))


# --- Effect ------------------------------------------------------------------


def test_effect_set_brightness_scales_each_channel():
    """set_brightness scales every palette color by the brightness factor."""
    mod = _load_effect_base()
    b = mod.Bitmap(2, 1)
    p = mod.Palette(3)
    p[0] = 0x000000
    p[1] = 0xFF8040  # R=255, G=128, B=64
    p[2] = 0xFFFFFF

    class _Fx(mod.Effect):
        bitmap = b
        palette = p
        scale = 1

    fx = _Fx()
    fx._init_render()

    fx.set_brightness(0.5)
    # R = 255*0.5 = 127, G = 128*0.5 = 64, B = 64*0.5 = 32
    assert p[1] == (127 << 16) | (64 << 8) | 32
    # White scales to half-white
    assert p[2] == (127 << 16) | (127 << 8) | 127
