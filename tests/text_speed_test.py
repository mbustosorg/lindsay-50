"""Tests for lib_shared.scroller_base.SPEED_TABLE + speed→pacing.

The user-facing knob is `speed` (1..5). The scroller translates it to
the underlying `frame_delay` / `offset_seconds` via SPEED_TABLE. This
file pins the table values and exercises `set_speed()`.
"""

import pytest

from lib_shared.scroller_base import ScrollerBase


def test_default_speed_is_3():
    """DEFAULT_SPEED is 3 (Medium)."""
    assert ScrollerBase.DEFAULT_SPEED == 3


def test_speed_table_has_five_entries():
    """SPEED_TABLE covers speeds 1..5 (index 0 = speed 1)."""
    assert len(ScrollerBase.SPEED_TABLE) == 5


def test_speed_labels_has_five_entries():
    """SPEED_LABELS is the same length as the table."""
    assert len(ScrollerBase.SPEED_LABELS) == 5


def test_speed_3_is_default_pacing():
    """Speed 3 (Medium) maps to (0.040, 1.0) — the historic defaults."""
    assert ScrollerBase.resolve_pacing(3) == (0.040, 1.0)


def test_speed_1_is_slowest_frame_delay():
    """Speed 1 (Low) has the largest frame_delay."""
    fd_1, _ = ScrollerBase.resolve_pacing(1)
    fd_2, _ = ScrollerBase.resolve_pacing(2)
    assert fd_1 > fd_2


def test_speed_5_is_fastest_frame_delay():
    """Speed 5 (High) has the smallest frame_delay."""
    fd_4, _ = ScrollerBase.resolve_pacing(4)
    fd_5, _ = ScrollerBase.resolve_pacing(5)
    assert fd_5 < fd_4


def test_table_is_monotonic():
    """frame_delay strictly decreases 1→5 (faster speed → smaller delay)."""
    delays = [ScrollerBase.resolve_pacing(s)[0] for s in range(1, 6)]
    assert delays == sorted(delays, reverse=True)
    # All strictly decreasing (no duplicates):
    assert all(delays[i] > delays[i + 1] for i in range(4))


def test_resolve_pacing_clamps_out_of_range():
    """0 and 6 raise ValueError (out of 1..5)."""
    with pytest.raises(ValueError):
        ScrollerBase.resolve_pacing(0)
    with pytest.raises(ValueError):
        ScrollerBase.resolve_pacing(6)


def test_resolve_pacing_rejects_non_int():
    """String and float raise ValueError."""
    with pytest.raises(ValueError):
        ScrollerBase.resolve_pacing("fast")
    with pytest.raises(ValueError):
        ScrollerBase.resolve_pacing(3.0)


def test_resolve_pacing_rejects_bool():
    """bool is an int subclass; must be rejected explicitly."""
    with pytest.raises(ValueError):
        ScrollerBase.resolve_pacing(True)
    with pytest.raises(ValueError):
        ScrollerBase.resolve_pacing(False)


def _stub_scroller(**kwargs):
    """Build a ScrollerBase with abstract methods stubbed out, suitable
    for constructor + set_speed assertions (we never call measure_text /
    draw_text / compute_layout in these tests)."""

    class _Stub(ScrollerBase):
        def measure_text(self, text):
            return len(text) * 6

        def draw_text(self, canvas, text, x, y, color):
            return None

        def compute_layout(self, canvas_width, canvas_height):
            self.single_line = True

    return _Stub(**kwargs)


def test_constructor_default_uses_speed_3():
    """No-arg construction uses speed=3 → (0.040, 1.0)."""
    s = _stub_scroller()
    assert s.frame_delay == 0.040
    assert s.offset_seconds == 1.0


def test_constructor_with_speed_5():
    """speed=5 → (0.020, 0.5)."""
    s = _stub_scroller(speed=5)
    assert s.frame_delay == 0.020
    assert s.offset_seconds == 0.5


def test_constructor_with_speed_1():
    """speed=1 → (0.080, 1.5)."""
    s = _stub_scroller(speed=1)
    assert s.frame_delay == 0.080
    assert s.offset_seconds == 1.5


def test_constructor_rejects_invalid_speed():
    """Constructor raises ValueError on out-of-range speed."""
    with pytest.raises(ValueError):
        _stub_scroller(speed=0)
    with pytest.raises(ValueError):
        _stub_scroller(speed=6)


def test_constructor_rejects_legacy_pacing_kwargs():
    """`frame_delay=` / `offset_seconds=` are no longer accepted.

    Speed is the only pacing knob on the public constructor; raw
    pacing numbers are no longer a supported back-compat path. The
    attributes `frame_delay` / `offset_seconds` still exist (so
    `set_speed()` can update them in place) but they're derived
    from `speed` via `SPEED_TABLE`.
    """
    with pytest.raises(TypeError, match="frame_delay"):
        _stub_scroller(frame_delay=0.123)
    with pytest.raises(TypeError, match="offset_seconds"):
        _stub_scroller(offset_seconds=2.5)


def test_set_speed_updates_pacing():
    """set_speed mutates frame_delay + offset_seconds in place."""
    s = _stub_scroller()
    s.set_speed(5)
    assert s.frame_delay == 0.020
    assert s.offset_seconds == 0.5
    s.set_speed(1)
    assert s.frame_delay == 0.080
    assert s.offset_seconds == 1.5


def test_set_speed_rejects_invalid():
    """set_speed raises ValueError on out-of-range / non-int / bool."""
    s = _stub_scroller()
    with pytest.raises(ValueError):
        s.set_speed(0)
    with pytest.raises(ValueError):
        s.set_speed(6)
    with pytest.raises(ValueError):
        s.set_speed(True)


def test_set_color_updates_color():
    """set_color mutates _color in place (used by config-envelope handlers)."""
    s = _stub_scroller(color=0xFF0000)
    s.set_color(0x00FF00)
    assert s._color == 0x00FF00
    # color_tuple reflects the new value.
    assert s.color_tuple() == (0, 0xFF, 0)
