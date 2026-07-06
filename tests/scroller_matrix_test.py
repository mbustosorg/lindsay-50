"""Tests for heart-matrix-controller.scroller.MatrixScroller.

The MatrixScroller subclasses ScrollerBase and wires it up to the rgbmatrix
graphics API (BDF font + DrawText). We mock the rgbmatrix module so the
test runs in any environment, then assert the inherited time/pixel math
matches the base class's behavior.
"""

import importlib.util
import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure project root is on the path so lib_shared is importable
_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# ---------------------------------------------------------------------------
# rgbmatrix stub: provide the symbols scroller.py imports at module level.
# Install once at import time so `from rgbmatrix import graphics` resolves.
# ---------------------------------------------------------------------------
_rgbmatrix = types.ModuleType("rgbmatrix")
_graphics = types.ModuleType("rgbmatrix.graphics")
_rgbmatrix.graphics = _graphics  # type: ignore[attr-defined]
sys.modules.setdefault("rgbmatrix", _rgbmatrix)
sys.modules.setdefault("rgbmatrix.graphics", _graphics)


def _make_mock_font(height=13, baseline=10):
    font = MagicMock()
    font.height = height
    font.baseline = baseline
    font.LoadFont = MagicMock()
    font.CharacterWidth = MagicMock(return_value=5)  # 5 px per char
    return font


def _font_factory():
    return _make_mock_font()


_graphics.Font = _font_factory  # type: ignore[attr-defined]


def _color_stub(r, g, b):
    c = MagicMock()
    c.r, c.g, c.b = r, g, b
    return c


_graphics.Color = MagicMock(side_effect=_color_stub)  # type: ignore[attr-defined]
_graphics.DrawText = MagicMock()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Scroller test fixture: stub display + load the module
# ---------------------------------------------------------------------------


class _StubDisplay:
    """Mimics the rgbmatrix Display just enough for MatrixScroller to init."""

    class _Canvas:
        width = 64
        height = 64

    canvas = _Canvas()
    width = 64
    height = 64


def _load_scroller_module():
    """Load heart-matrix-controller/scroller.py via importlib (hyphen-safe)."""
    scroller_path = _PROJECT_ROOT / "heart-matrix-controller" / "scroller.py"
    spec = importlib.util.spec_from_file_location("heart_matrix_controller.scroller", str(scroller_path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["heart_matrix_controller.scroller"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fresh_module(monkeypatch):
    """Re-import scroller.py with a stubbed get_config so each test is fresh.

    The scroller module reads FONT_PATH via get_config() at __init__ time;
    we point get_config at a MagicMock with no FONT_PATH so the constructor
    falls back to the hardcoded 'fonts/6x9.bdf' (never read because
    rgbmatrix.graphics.Font is mocked).
    """
    cfg_mock = MagicMock()
    cfg_mock.if_exists = lambda key: None
    monkeypatch.setattr("lib_shared.config_reader.get_config", lambda *a, **k: cfg_mock)
    # Reset rgbmatrix DrawText mock so call counts are per-test
    _graphics.DrawText.reset_mock()
    return _load_scroller_module()


def test_matrix_scroller_inherits_scroller_base(fresh_module):
    """MatrixScroller is a ScrollerBase subclass — the shared math applies."""
    from lib_shared.scroller_base import ScrollerBase

    assert issubclass(fresh_module.MatrixScroller, ScrollerBase)


def test_matrix_scroller_init_loads_font_and_sets_layout(fresh_module):
    """After init, top_y / bottom_y / single_line are populated for 64x64.

    The device renders a single orange line centered on the full display
    (see MatrixScroller.compute_layout — "Always one line, centered
    vertically on the whole display."). 64x64 panels show the same single
    line as a 32x32 panel, not two lines stacked.
    """
    s = fresh_module.MatrixScroller(_StubDisplay())
    # Single-line layout: top_y == bottom_y, both centered on the panel.
    assert s.single_line is True
    assert s.top_y == s.bottom_y
    # baseline_for(canvas_height // 2) = 32 + 10 - 6 = 36
    assert s.top_y == 36


def test_matrix_scroller_set_text_initializes_positions(fresh_module):
    """set_text with a width populates text, text_width, and x positions."""
    s = fresh_module.MatrixScroller(_StubDisplay())
    s.set_text("hello", canvas_width=64)
    assert s.text == "hello"
    assert s.text_width == 25  # 5 chars * 5 px each
    assert s.top_x == 64
    assert s.bottom_x == 64


def test_matrix_scroller_tick_advances_x_by_expected_pixels(fresh_module, monkeypatch):
    """0.5s @ frame_delay=0.05 -> 10 pixels of motion, matching the base class."""
    state = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: state["t"])

    s = fresh_module.MatrixScroller(_StubDisplay())
    s.frame_delay = 0.05  # direct attr assignment; speed= kwarg is the public path
    s.set_text("hi", canvas_width=64)
    initial_top = s.top_x

    state["t"] += 0.5
    s.tick(canvas_width=64)
    assert s.top_x == initial_top - 10


def test_matrix_scroller_render_calls_draw_text(fresh_module):
    """render(canvas) blits the text into the rgbmatrix canvas via DrawText.

    Single-line layout: exactly one DrawText call per frame.
    """
    s = fresh_module.MatrixScroller(_StubDisplay())
    s.set_text("hi", canvas_width=64)
    fake_canvas = MagicMock()
    s.render(fake_canvas)
    assert _graphics.DrawText.call_count == 1
    args, _ = _graphics.DrawText.call_args
    assert args[0] is fake_canvas
    # signature: DrawText(canvas, font, x, y, color, text) -> text is args[5]
    assert args[5] == "hi"


def test_matrix_scroller_color_obj_scales_by_brightness(fresh_module):
    """set_brightness propagates into the rgbmatrix Color object."""
    s = fresh_module.MatrixScroller(_StubDisplay(), color=0xFF8040)
    s.set_brightness(0.5)
    color = s._color_obj()
    # 0xFF * 0.5 = 127, 0x80 * 0.5 = 64, 0x40 * 0.5 = 32
    assert color.r == 127
    assert color.g == 64
    assert color.b == 32


def test_matrix_scroller_falls_back_to_vendored_when_loadfont_raises(monkeypatch):
    """A FONT_PATH pointing at a missing file should fall back to the vendored
    font instead of crashing the boot path. This is the issue #49 v1→v2
    onboarding hazard: a legacy settings.toml may still name a font path that
    no longer ships with the repo (e.g. the old rpi-rgb-led-matrix
    "../../fonts/8x13.bdf"). On LoadFont exception we retry with the vendored
    "fonts/6x9.bdf" before raising."""
    # Configure FONT_PATH to a known-broken path.
    cfg_mock = MagicMock()
    cfg_mock.if_exists = lambda key: "/nonexistent/8x13.bdf" if key == "FONT_PATH" else None

    # fresh_module builds a per-test font; we want LoadFont to raise the
    # first time and succeed thereafter, so the catch+retry path runs once.
    font = _make_mock_font()
    font.LoadFont.side_effect = [Exception("Couldn't load font 8x13.bdf"), None]
    monkeypatch.setattr(_graphics, "Font", lambda: font)
    monkeypatch.setattr("lib_shared.config_reader.get_config", lambda *a, **k: cfg_mock)

    mod = _load_scroller_module()
    s = mod.MatrixScroller(_StubDisplay())  # should NOT raise

    # First call with the broken FONT_PATH, second call with vendored fallback.
    assert font.LoadFont.call_count == 2
    args = font.LoadFont.call_args_list
    assert args[0].args[0] == "/nonexistent/8x13.bdf"
    assert args[1].args[0] == "fonts/6x9.bdf"
    assert s.single_line is True  # layout was still populated post-fallback


def test_matrix_scroller_raises_when_vendored_also_unavailable(monkeypatch):
    """If even the vendored fallback fails, the scroller must propagate the
    error — we don't want silent black-screen fallbacks that mask a real
    font installation problem on the Pi."""
    cfg_mock = MagicMock()
    cfg_mock.if_exists = lambda key: None  # forces vendored default

    font = _make_mock_font()
    font.LoadFont.side_effect = Exception("Couldn't load font 6x9.bdf")
    monkeypatch.setattr(_graphics, "Font", lambda: font)
    monkeypatch.setattr("lib_shared.config_reader.get_config", lambda *a, **k: cfg_mock)

    mod = _load_scroller_module()
    with pytest.raises(Exception, match="Couldn't load font"):
        mod.MatrixScroller(_StubDisplay())
    # Only one LoadFont call: the failing vendored fallback is not retried.
    assert font.LoadFont.call_count == 1
