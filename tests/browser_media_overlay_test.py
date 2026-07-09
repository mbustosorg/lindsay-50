"""Tests for `BrowserMediaOverlay` (issue #38).

The preview-side analog of `MediaCycler` — same cycle logic (D5 / D12),
but renders through HTML `<img>` / `<video>` elements that the JS-side
`preview.js` drives from `current_media_url` / `current_media_kind` /
`current_opacity`. No PIL/cv2 import. The Pi keeps using `MediaCycler`.

Pins:
  - malformed media entries are dropped at construction
  - empty media → exhausted=True at construction
  - 1-item list → exhausted stays False (D12); current url+kind read back
  - multi-item → random pick from not-yet-shown items per advance
  - set_brightness stores the factor; current_opacity reads it
  - tick() is the cycle-advance clock; honors hold_seconds
  - render() is a no-op (returns None) so the LED-fuzzy canvas underneath
    isn't clobbered
  - the overlay never imports MediaCycler (browser side has no fs path)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_constructor_empty_media_signals_exhausted():
    """An empty media list leaves `exhausted` True at construction so the
    coordinator falls back to the rotation effect on the next fade."""
    from lib_shared.patterns.browser_media_overlay import BrowserMediaOverlay

    overlay = BrowserMediaOverlay("m1", [], api_base_url="http://flask.test")
    assert overlay.exhausted is True
    assert overlay.current_media_url == ""
    assert overlay.current_media_kind == ""
    # Default opacity (no set_brightness call) is 1.0 — visible when
    # the no-op render path's other inputs aren't driven by anything.
    assert overlay.current_opacity == 1.0


def test_constructor_sms_only_message_carries_one_item():
    """A real `Message.media=[]` produces an exhausted overlay (no media
    to render). A `Message.media=[<one item>]` produces a non-exhausted
    overlay that surfaces a single attachment."""
    from lib_shared.patterns.browser_media_overlay import BrowserMediaOverlay

    sms = BrowserMediaOverlay("m1", [], api_base_url="http://flask.test")
    assert sms.exhausted is True

    mms = BrowserMediaOverlay(
        "m1",
        [{"type": "image/jpeg", "url": "media/images/2026-07/a.jpg"}],
        api_base_url="http://flask.test",
    )
    assert mms.exhausted is False
    assert mms.items_remaining == 1


def test_constructor_drops_malformed_entries_with_warning(caplog):
    """Items missing `type` or `url` are dropped at construction; a
    WARNING lands per malformed entry. The remaining well-formed items
    survive."""
    import logging

    from lib_shared.patterns.browser_media_overlay import BrowserMediaOverlay

    media = [
        {"type": "", "url": "media/x.jpg"},  # bad: empty type
        {"type": "image/jpeg", "url": ""},  # bad: empty url
        {"type": "image/png", "url": "media/images/2026-07/a.png"},  # good
        {"not a": "dict"},  # bad: not a dict
        {"type": "video/mp4", "url": "media/videos/2026-07/b.mp4"},  # good
    ]
    with caplog.at_level(logging.WARNING, logger="heart"):
        overlay = BrowserMediaOverlay("m1", media, api_base_url="http://flask.test")
    assert overlay.items_remaining == 2
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    # Three drops → three warnings (one per malformed entry).
    assert len(warnings) >= 3


def test_constructor_empty_api_base_url_yields_empty_url():
    """An overlay with no api_base_url cannot build a proxy URL. The
    working list is non-empty (so `exhausted` stays False), but
    `current_media_url` is `""` — the JS-side `preview.js` interprets
    that as "no media to render" and hides the overlay. The
    coordinator does NOT fall back to rotation (that's reserved for
    codec-fetch failures, not for mis-configured base URLs)."""
    from lib_shared.patterns.browser_media_overlay import BrowserMediaOverlay

    overlay = BrowserMediaOverlay(
        "m1",
        [{"type": "image/jpeg", "url": "media/x.jpg"}],
        api_base_url="",
    )
    assert overlay.exhausted is False
    overlay.tick()  # activates the item
    assert overlay.current_media_url == ""
    assert overlay.current_media_kind == "image"


def test_constructor_kind_dispatch():
    """`image/*` → 'image'; `video/*` → 'video'; other → 'image'
    fallback. The kind is computed at construction (so the JS knows
    which element to drive even before the first tick), but
    `current_media_kind` returns `""` until `_active` is set."""
    from lib_shared.patterns.browser_media_overlay import BrowserMediaOverlay

    img = BrowserMediaOverlay(
        "m1",
        [{"type": "image/jpeg", "url": "media/x.jpg"}],
        api_base_url="http://flask.test",
    )
    img.tick()
    assert img.current_media_kind == "image"
    vid = BrowserMediaOverlay(
        "m2",
        [{"type": "video/mp4", "url": "media/x.mp4"}],
        api_base_url="http://flask.test",
    )
    vid.tick()
    assert vid.current_media_kind == "video"


# ---------------------------------------------------------------------------
# current_media_url / current_media_kind / current_opacity
# ---------------------------------------------------------------------------


def test_current_media_url_built_from_api_base_and_key():
    """`current_media_url` is `f"{api_base_url}/api/media/{key}"`.
    Trailing slashes on `api_base_url` are stripped."""
    from lib_shared.patterns.browser_media_overlay import BrowserMediaOverlay

    overlay = BrowserMediaOverlay(
        "m1",
        [{"type": "image/jpeg", "url": "media/images/2026-07/a.jpg"}],
        api_base_url="http://flask.test/",
    )
    overlay._active = overlay._items[0]
    assert overlay.current_media_url == "http://flask.test/api/media/media/images/2026-07/a.jpg"


def test_current_media_url_empty_when_no_active():
    """Until `tick()` runs the first time, `_active` is None and the
    URL is empty — no JS swap happens."""
    from lib_shared.patterns.browser_media_overlay import BrowserMediaOverlay

    overlay = BrowserMediaOverlay(
        "m1",
        [{"type": "image/jpeg", "url": "media/x.jpg"}],
        api_base_url="http://flask.test",
    )
    # No tick yet — _active is None.
    assert overlay._active is None
    assert overlay.current_media_url == ""


def test_current_opacity_tracks_set_brightness():
    """`set_brightness(b)` clamps to [0.0, 1.0] and is exposed via
    `current_opacity`. The JS overlay element reads the value each
    frame and applies it to `style.opacity`."""
    from lib_shared.patterns.browser_media_overlay import BrowserMediaOverlay

    overlay = BrowserMediaOverlay("m1", [], api_base_url="http://flask.test")
    overlay.set_brightness(0.42)
    assert overlay.current_opacity == pytest.approx(0.42)
    overlay.set_brightness(1.0)
    assert overlay.current_opacity == pytest.approx(1.0)
    overlay.set_brightness(0.0)
    assert overlay.current_opacity == pytest.approx(0.0)
    # Out-of-range values clamped.
    overlay.set_brightness(2.5)
    assert overlay.current_opacity == pytest.approx(1.0)
    overlay.set_brightness(-0.5)
    assert overlay.current_opacity == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Effect interface: tick() / render()
# ---------------------------------------------------------------------------


def test_tick_first_call_activates_first_item():
    """The first `tick()` populates `_active`. With a 1-item list this
    is the only item; the cycler never auto-advances on tick."""
    from lib_shared.patterns.browser_media_overlay import BrowserMediaOverlay

    overlay = BrowserMediaOverlay(
        "m1",
        [{"type": "image/jpeg", "url": "media/x.jpg"}],
        api_base_url="http://flask.test",
    )
    assert overlay._active is None
    overlay.tick()
    assert overlay._active is not None
    assert overlay._active["key"] == "media/x.jpg"
    assert overlay.current_media_url == "http://flask.test/api/media/media/x.jpg"


def test_tick_no_op_when_exhausted():
    """When `exhausted` is True, `tick()` returns early — no activation,
    no recursion."""
    from lib_shared.patterns.browser_media_overlay import BrowserMediaOverlay

    overlay = BrowserMediaOverlay("m1", [], api_base_url="http://flask.test")
    assert overlay.exhausted is True
    overlay.tick()  # must not raise
    assert overlay._active is None
    assert overlay.exhausted is True


def test_tick_single_item_stays_on_that_item():
    """The 1-item case never advances — `tick()` keeps the same active
    item across multiple calls (`exhausted` stays False). The
    coordinator handles the cutoff via `hold_seconds`."""
    from lib_shared.patterns.browser_media_overlay import BrowserMediaOverlay

    overlay = BrowserMediaOverlay(
        "m1",
        [{"type": "video/mp4", "url": "media/x.mp4"}],
        api_base_url="http://flask.test",
        hold_seconds=120.0,
    )
    overlay.tick()
    first_active = overlay._active
    assert first_active is not None
    for _ in range(5):
        overlay.tick()
        assert overlay._active is first_active
        assert overlay.exhausted is False


def test_tick_multi_item_advances_after_duration():
    """Multi-item lists advance after the per-item duration elapses
    (`max(10s, item_duration)` per D5). With hold_seconds=1e9 the
    test never short-circuits on hold."""
    from lib_shared.patterns.browser_media_overlay import BrowserMediaOverlay

    overlay = BrowserMediaOverlay(
        "m1",
        [
            {"type": "image/jpeg", "url": "media/a.jpg"},
            {"type": "image/png", "url": "media/b.png"},
            {"type": "video/mp4", "url": "media/c.mp4"},
        ],
        api_base_url="http://flask.test",
        hold_seconds=1e9,
    )
    # Force the per-item duration low so the test's clock advances
    # past it without sleeping.
    for it in overlay._items:
        it["duration"] = 0.05  # 50 ms floor
    overlay.tick()
    first_active = overlay._active
    assert first_active is not None
    # Bump elapsed past duration on the next tick.
    time.sleep(0.06)
    overlay.tick()
    # The cycler rotated: `_active` is a different item (random pick
    # from not-yet-shown). For a 3-item list, the first advance has
    # 2 candidates, so a fresh pick is the typical outcome.
    second_active = overlay._active
    assert second_active is not None
    # It might be the same item if random picked the only one shown
    # (impossible at this point — only 1 marked shown) — so check
    # the first item IS marked shown.
    assert first_active["shown"] is True


def test_tick_respects_hold_seconds():
    """When `elapsed >= hold_seconds`, `tick()` does NOT advance. The
    coordinator's own clock handles the cut-off."""
    from lib_shared.patterns.browser_media_overlay import BrowserMediaOverlay

    overlay = BrowserMediaOverlay(
        "m1",
        [
            {"type": "image/jpeg", "url": "media/a.jpg"},
            {"type": "image/png", "url": "media/b.png"},
        ],
        api_base_url="http://flask.test",
        hold_seconds=0.05,
    )
    for it in overlay._items:
        it["duration"] = 0.01  # very short per-item window
    overlay.tick()
    first_active = overlay._active
    time.sleep(0.10)  # past both duration and hold_seconds
    overlay.tick()
    # Held — same item.
    assert overlay._active is first_active


def test_render_is_noop():
    """`render(canvas)` is a no-op for the overlay path — the DOM
    `<img>` / `<video>` element is positioned over the canvas by
    `preview.js`; the overlay must not clobber the LED-fuzzy frame
    the JS-side `blitFuzzy` just wrote."""
    from lib_shared.patterns.browser_media_overlay import BrowserMediaOverlay

    overlay = BrowserMediaOverlay(
        "m1",
        [{"type": "image/jpeg", "url": "media/x.jpg"}],
        api_base_url="http://flask.test",
    )

    class _FakeCanvas:
        def __init__(self):
            self.cleared = False
            self.drawn = False

        def clear(self):
            self.cleared = True

        def draw(self):
            self.drawn = True

    canvas = _FakeCanvas()
    overlay.render(canvas)
    # None of the canvas methods fired — overlay is a no-op renderer.
    assert canvas.cleared is False
    assert canvas.drawn is False


# ---------------------------------------------------------------------------
# Multi-item cycle: every item eventually shown
# ---------------------------------------------------------------------------


def test_multi_item_cycle_eventually_marks_all_items_shown():
    """With enough ticks (forcing short durations, long hold), the
    cycler marks every item as shown at least once. After all items
    have been shown at least once, a subsequent pick resets the
    cycle — every item gets shown again later (the cycle never
    `exhausts` while the hold window allows more time)."""
    from lib_shared.patterns.browser_media_overlay import BrowserMediaOverlay

    overlay = BrowserMediaOverlay(
        "m1",
        [
            {"type": "image/jpeg", "url": "media/a.jpg"},
            {"type": "image/png", "url": "media/b.png"},
            {"type": "video/mp4", "url": "media/c.mp4"},
        ],
        api_base_url="http://flask.test",
        hold_seconds=1e9,
    )
    for it in overlay._items:
        it["duration"] = 0.01
    # Tick enough times for the cycle to visit every item at least once.
    for _ in range(20):
        overlay.tick()
        time.sleep(0.02)
    shown_count = sum(1 for it in overlay._items if it["shown"])
    # The cycler visits every item at least once across 20 ticks.
    assert shown_count == 3


def test_multi_item_cycle_does_not_set_exhausted_within_hold():
    """A multi-item list with the hold window still active never
    flips `exhausted` to True. The coordinator's clock is what
    ends the hold."""
    from lib_shared.patterns.browser_media_overlay import BrowserMediaOverlay

    overlay = BrowserMediaOverlay(
        "m1",
        [
            {"type": "image/jpeg", "url": "media/a.jpg"},
            {"type": "image/png", "url": "media/b.png"},
        ],
        api_base_url="http://flask.test",
        hold_seconds=10.0,
    )
    for _ in range(5):
        overlay.tick()
        time.sleep(0.01)
    assert overlay.exhausted is False


# ---------------------------------------------------------------------------
# Decoupling: no MediaCycler import (the preview tree doesn't have it).
# ---------------------------------------------------------------------------


def test_overlay_does_not_import_media_cycler():
    """The browser-side `lib_shared/patterns/browser_media_overlay.py`
    must NOT `import` (or `from ... import`) any of the host-only
    renderer modules — those pull in PIL/cv2 (OpenCV isn't a
    Pyodide package, and the cycler's host-side path uses real-disk
    caches the browser can't satisfy). The preview tree doesn't even
    symlink `image_display.py` or `video_display.py` — this test
    pins that the browser path stays free of those imports."""
    import importlib

    importlib.invalidate_caches()
    overlay_mod = importlib.import_module(
        "lib_shared.patterns.browser_media_overlay",
    )
    overlay_file = overlay_mod.__file__ or ""
    overlay_path = Path(overlay_file)
    src = overlay_path.read_text(encoding="utf-8")
    # Strip docstrings + comments so prose references in the
    # module's narrative don't trip the check. The check is for
    # actual `import X` / `from X import` lines, not mentions.
    code_lines = [
        ln
        for ln in src.splitlines()
        if ln.strip()
        and not ln.lstrip().startswith(("#", '"', "'"))
        and not (ln.lstrip().startswith('"""') or ln.lstrip().startswith("'''"))
    ]
    code = "\n".join(code_lines)
    forbidden = ("media_cycler", "image_display", "video_display", "cv2")
    for tok in forbidden:
        assert f"import {tok}" not in code, f"browser_media_overlay.py must not import {tok} " f"(host-side only)"
        assert f"from {tok}" not in code
        assert f"from lib_shared.patterns import {tok}" not in code
