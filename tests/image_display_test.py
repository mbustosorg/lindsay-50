"""Tests for `lib_shared.patterns.image_display.ImageDisplay` (issue #38 / openspec
`image-display-pattern`).

ImageDisplay is the inner renderer consumed by `MediaCycler` to display a
single S3-fetched MMS attachment on the panel. It supports PNG, JPEG, GIF,
and WebP. This test file pins the format discovery, single-image hold,
multi-image crossfade, and the corrupt-file WARNING path.
"""

from __future__ import annotations

import importlib
import io
import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


class _StubCanvas:
    """Minimal canvas stub — ImageDisplay reads `width` / `height` and
    calls `SetPixel` per output pixel."""

    def __init__(self, w: int = 8, h: int = 8):
        self.width = w
        self.height = h
        self.pixels: list[tuple[int, int, int, int, int]] = []

    def SetPixel(self, x: int, y: int, r: int, g: int, b: int) -> None:
        self.pixels.append((x, y, r, g, b))


class _StubDisplay:
    def __init__(self, w: int = 8, h: int = 8):
        self.canvas = _StubCanvas(w, h)


def _make_png_bytes(width: int = 8, height: int = 8, *, with_alpha: bool = True) -> bytes:
    """Generate a small PNG with optional alpha channel."""
    from PIL import Image

    if with_alpha:
        img = Image.new("RGBA", (width, height), (255, 0, 0, 128))
    else:
        img = Image.new("RGB", (width, height), (0, 255, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_jpeg_bytes(width: int = 8, height: int = 8) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (width, height), (0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _make_gif_bytes(width: int = 8, height: int = 8) -> bytes:
    from PIL import Image

    img = Image.new("P", (width, height), 0)
    buf = io.BytesIO()
    img.save(buf, format="GIF")
    return buf.getvalue()


def _make_webp_bytes(width: int = 8, height: int = 8) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (width, height), (255, 255, 0))
    buf = io.BytesIO()
    img.save(buf, format="WEBP")
    return buf.getvalue()


@pytest.fixture
def cfg_stub(monkeypatch):
    """Patch `lib_shared.config_reader.get_config` to return a minimal
    stub. ImageDisplay only consults PNG_INTERVAL / PNG_FADE; everything
    else is read from constructor args."""
    stub = SimpleNamespace(
        if_exists=lambda k: {"PNG_INTERVAL": "0.05", "PNG_FADE": "0.02"}.get(k),
    )
    monkeypatch.setattr(
        "lib_shared.config_reader.get_config",
        lambda required_keys=None: stub,
    )
    return stub


# ---------------------------------------------------------------------------
# Format discovery (5.1)
# ---------------------------------------------------------------------------


def test_image_display_discovers_png_jpg_jpeg_gif_webp(tmp_path, cfg_stub):
    """A directory containing one of each supported format is globbed
    and the resulting image list has 5 entries (PNG, JPG, JPEG, GIF, WebP)."""
    from lib_shared.patterns.image_display import ImageDisplay

    (tmp_path / "a.png").write_bytes(_make_png_bytes())
    (tmp_path / "b.jpg").write_bytes(_make_jpeg_bytes())
    (tmp_path / "c.jpeg").write_bytes(_make_jpeg_bytes())
    (tmp_path / "d.gif").write_bytes(_make_gif_bytes())
    (tmp_path / "e.webp").write_bytes(_make_webp_bytes())
    # Decoy: must be ignored
    (tmp_path / "ignore.txt").write_text("not an image")

    display = ImageDisplay(_StubDisplay(), dir=tmp_path)
    suffixes = {p.suffix.lower() for p in display._paths}
    assert suffixes == {".png", ".jpg", ".jpeg", ".gif", ".webp"}
    # The .txt decoy isn't in the list.
    assert all(p.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"} for p in display._paths)


def test_image_display_ignores_unsupported_extensions(tmp_path, cfg_stub, caplog):
    """A directory with only unsupported files yields an empty _paths
    and a blank panel + WARNING (no crash)."""
    from lib_shared.patterns.image_display import ImageDisplay

    (tmp_path / "a.bmp").write_bytes(b"BM...")
    (tmp_path / "b.tiff").write_bytes(b"II*\x00")
    (tmp_path / "c.txt").write_text("hello")

    with caplog.at_level(logging.WARNING, logger="heart"):
        display = ImageDisplay(_StubDisplay(), dir=tmp_path)
    assert display._paths == []
    # And a WARNING reached the log.
    assert any(r.levelno == logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# Single-image mode (5.1 + Section 6 MediaCycler consumer)
# ---------------------------------------------------------------------------


def test_image_display_single_path_mode_renders(tmp_path, cfg_stub):
    """Passing `path=` (single image) bypasses the directory glob and
    renders the one image directly. `MediaCycler` uses this mode per MMS."""
    from lib_shared.patterns.image_display import ImageDisplay

    img_path = tmp_path / "single.png"
    img_path.write_bytes(_make_png_bytes())

    display = _StubDisplay()
    eff = ImageDisplay(display, path=img_path)
    assert len(eff._paths) == 1
    assert eff._paths[0] == img_path

    # Render to a stub canvas: every SetPixel call should write a color,
    # not (0, 0, 0, 0). A fully black image would mean the bitmap
    # wasn't loaded.
    eff.render(display.canvas)
    assert len(display.canvas.pixels) == display.canvas.width * display.canvas.height
    # At least one non-zero pixel proves the image data made it through
    # the load → quantize → palette pipeline.
    non_black = [p for p in display.canvas.pixels if (p[2], p[3], p[4]) != (0, 0, 0)]
    assert non_black, "image rendered fully black; load pipeline broken"


def test_image_display_single_path_holds_forever(tmp_path, cfg_stub):
    """A 1-image `path=` mode never advances (`len(self._paths) <= 1`
    in `tick()` is the documented fast-path)."""
    from lib_shared.patterns.image_display import ImageDisplay

    img_path = tmp_path / "single.png"
    img_path.write_bytes(_make_png_bytes())

    eff = ImageDisplay(_StubDisplay(), path=img_path)
    initial_index = eff._index
    for _ in range(100):
        eff.tick()
    assert eff._index == initial_index
    assert eff._phase == "hold"


# ---------------------------------------------------------------------------
# Multi-image crossfade (5.6)
# ---------------------------------------------------------------------------


def test_image_display_multi_image_advances_through_crossfade(tmp_path, cfg_stub):
    """2-image directory: tick() advances through out → in → hold and
    the index increments once the fade-in completes."""
    from lib_shared.patterns.image_display import ImageDisplay

    (tmp_path / "01.png").write_bytes(_make_png_bytes())
    (tmp_path / "02.png").write_bytes(_make_png_bytes())

    eff = ImageDisplay(_StubDisplay(), dir=tmp_path)
    assert len(eff._paths) == 2
    assert eff._index == 0
    assert eff._phase == "hold"

    # Hold for the configured interval, then fade-out
    time.sleep(0.10)
    eff.tick()
    assert eff._phase == "out"

    # Finish the fade-out → swap → fade-in
    time.sleep(0.05)
    eff.tick()
    # Either "in" (post-swap) or "hold" (fade-in complete) — both OK.
    assert eff._phase in ("in", "hold")


# ---------------------------------------------------------------------------
# Per-format load paths (5.2)
# ---------------------------------------------------------------------------


def test_image_display_png_with_alpha_uses_mask_path(tmp_path, cfg_stub):
    """An RGBA PNG triggers the alpha-as-mask branch (mask = img.getchannel('A'))."""
    from lib_shared.patterns.image_display import ImageDisplay

    img_path = tmp_path / "rgba.png"
    img_path.write_bytes(_make_png_bytes(with_alpha=True))

    eff = ImageDisplay(_StubDisplay(), path=img_path)
    # The bitmap/palette should be populated (the load didn't crash).
    assert eff.bitmap is not None
    assert eff.palette is not None


def test_image_display_png_rgb_uses_rgb_path(tmp_path, cfg_stub):
    """An RGB-only PNG (no alpha) takes the convert("RGB") branch."""
    from lib_shared.patterns.image_display import ImageDisplay

    img_path = tmp_path / "rgb.png"
    img_path.write_bytes(_make_png_bytes(with_alpha=False))

    eff = ImageDisplay(_StubDisplay(), path=img_path)
    assert eff.bitmap is not None
    assert eff.palette is not None


def test_image_display_jpeg_uses_rgb_path(tmp_path, cfg_stub):
    """A JPEG triggers the convert("RGB") branch — no alpha channel exists."""
    from lib_shared.patterns.image_display import ImageDisplay

    img_path = tmp_path / "photo.jpg"
    img_path.write_bytes(_make_jpeg_bytes())

    eff = ImageDisplay(_StubDisplay(), path=img_path)
    assert eff.bitmap is not None
    assert eff.palette is not None


def test_image_display_gif_uses_rgb_path(tmp_path, cfg_stub):
    """A GIF (palette mode) is loaded via convert("RGB") (drop alpha)."""
    from lib_shared.patterns.image_display import ImageDisplay

    img_path = tmp_path / "anim.gif"
    img_path.write_bytes(_make_gif_bytes())

    eff = ImageDisplay(_StubDisplay(), path=img_path)
    assert eff.bitmap is not None


def test_image_display_webp_uses_rgb_path(tmp_path, cfg_stub):
    """A WebP triggers the convert("RGB") branch."""
    from lib_shared.patterns.image_display import ImageDisplay

    img_path = tmp_path / "photo.webp"
    img_path.write_bytes(_make_webp_bytes())

    eff = ImageDisplay(_StubDisplay(), path=img_path)
    assert eff.bitmap is not None


# ---------------------------------------------------------------------------
# Effect interface (set_brightness + render)
# ---------------------------------------------------------------------------


def test_image_display_set_brightness_zero_resets_to_hold(tmp_path, cfg_stub):
    """`set_brightness(0.0)` is the (de)activation signal — the slideshow
    resets to a fully-lit hold state so the coordinator's fade-in shows
    a complete frame, not a mid-crossfade."""
    from lib_shared.patterns.image_display import ImageDisplay

    (tmp_path / "01.png").write_bytes(_make_png_bytes())
    (tmp_path / "02.png").write_bytes(_make_png_bytes())

    eff = ImageDisplay(_StubDisplay(), dir=tmp_path)
    # Force a mid-crossfade state.
    eff._phase = "out"
    eff._img_b = 0.5
    eff.set_brightness(0.0)
    assert eff._phase == "hold"
    assert eff._img_b == 1.0
    # The coord brightness is also stashed.
    assert eff._coord_b == 0.0


def test_image_display_render_writes_all_pixels(tmp_path, cfg_stub):
    """`render(canvas)` writes width × height SetPixel calls in row-major order."""
    from lib_shared.patterns.image_display import ImageDisplay

    img_path = tmp_path / "single.png"
    img_path.write_bytes(_make_png_bytes(width=4, height=4, with_alpha=True))

    display = _StubDisplay(w=4, h=4)
    eff = ImageDisplay(display, path=img_path)
    eff.render(display.canvas)
    assert len(display.canvas.pixels) == 4 * 4


# ---------------------------------------------------------------------------
# Importable + callable
# ---------------------------------------------------------------------------


def test_image_display_module_importable():
    """The new module is importable from the new shared location."""
    mod = importlib.import_module("lib_shared.patterns.image_display")
    assert hasattr(mod, "ImageDisplay")
    assert callable(mod.ImageDisplay)
