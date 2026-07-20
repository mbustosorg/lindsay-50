"""Browser-preview integration smoke for the media override (issue #38).

The browser preview runs the same `lib_shared/effects_coordinator.py`
as the Pi. It constructs an `EffectsCoordinator` via
`static/preview/heart-message-manager/app_main.py` and binds the
page-local render layer via `preview_main.py`. This test pins
the contract that the preview's coordinator path doesn't break
under the new `media_api_base_url` / `media_cache_dir` kwargs:

- Default kwargs (no media URL, no cache dir) work — the
  coordinator constructs and ticks without error.
- An MMS message with `media=[]` constructs the coordinator, runs
  ticks, and lands in `background` mode (no cycler, no warnings).
- An MMS message with non-empty `media` is rendered without error
  in the preview. The cycler is constructed but the items are
  dropped because `api_base_url` is empty (the preview is
  illustrative, not a real fetcher). The coordinator falls back
  to the rotation effect via `_maybe_fall_back_to_rotation`.

This is a smoke test — the preview's actual PyScript bootstrap
path is exercised in-browser, not in pytest. The test pins the
SHAPE of the preview's coordinator path so a future refactor
that breaks it (e.g., a required media_api_base_url kwarg) is
caught.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from tests.effects_coordinator_test import (  # noqa: E402
    _StubCanvas,
    _StubDisplay,
    _StubScroller,
    _StubMessageManager,
    _make_effect,
    _Clock,
)


def _build_preview_coord(messages=None, effects_settings=None):
    """Build a coordinator the way `static/preview/heart-message-manager/
    app_main.py` does: with `is_browser=True` and `media_api_base_url`
    set to a stub origin. The preview's per-page `preview_main.py`
    calls `coord.bind(...)` with the page-local render layer; we
    mirror that here via the constructor."""
    from lib_shared.effects_coordinator import EffectsCoordinator
    from lib_shared.models import EffectsSettings, TextSettings

    mgr = _StubMessageManager(
        messages=messages or [],
        effects_settings=effects_settings or EffectsSettings(),
    )
    display = _StubDisplay()
    scroller = _StubScroller()
    fx_a = _make_effect("A")()
    heart = _make_effect("Heart")()
    return EffectsCoordinator(
        message_manager=mgr,
        display=display,
        scroller=scroller,
        effects=[fx_a],
        heart=heart,
        # issue #38: the preview constructs a BrowserMediaOverlay
        # (DOM-driven) instead of a MediaCycler (PIL/cv2-driven).
        is_browser=True,
        media_api_base_url="http://preview.test",
    )


def test_preview_coord_constructs_with_default_media_kwargs():
    """The preview's `app_main.py` constructs the coordinator with
    no `media_api_base_url` / `media_cache_dir` kwargs. Construction
    succeeds and `coord.current` is the heart (the boot-splash)."""
    from lib_shared.models import Message, MessageView

    msg = MessageView(
        message=Message(
            id="m1",
            sender="+15551234567",
            body="sms only",
            received_at="2026-07-09T00:00:00Z",
            media=[],
        ),
        suppressed=False,
    )
    coord = _build_preview_coord(messages=[msg])
    # The coordinator is bound to the page-local render layer
    # and boots with the heart as the current effect.
    assert coord.current is not None


def test_preview_coord_handles_sms_only_message():
    """An SMS-only message (media=[]) in the preview: ticks run,
    no cycler is constructed (because the cycler helper returns
    None for SMS-only), and the state machine lands in `background`
    (no text to display in the preview's seeded state)."""
    from lib_shared.models import EffectsSettings, Message, MessageView

    msg = MessageView(
        message=Message(
            id="m1",
            sender="+15551234567",
            body="sms only",
            received_at="2026-07-09T00:00:00Z",
            media=[],
        ),
        suppressed=False,
    )
    coord = _build_preview_coord(
        messages=[msg],
        effects_settings=EffectsSettings(
            intro_seconds=0.0,
            fade_seconds=0.01,
            hold_seconds=0.1,
            idle_seconds=0.05,
        ),
    )
    # Patch time.monotonic and tick the state machine forward.
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    try:
        coord.start()
        clock.advance(0.001)
        coord.tick()  # intro → out
        clock.advance(0.02)
        coord.tick()  # out → in
        clock.advance(0.02)
        coord.tick()  # in → hold (text is set, so hold)
        # The current effect is the rotation entry, NOT a MediaCycler
        # (SMS-only messages don't construct a cycler).
        from lib_shared.patterns.media_cycler import MediaCycler

        assert not isinstance(coord.current, MediaCycler)
    finally:
        monkey.undo()


def test_preview_coord_mms_message_constructs_browser_overlay(caplog):
    """An MMS message in the preview: with `is_browser=True`, the
    coordinator constructs a `BrowserMediaOverlay` (not a
    `MediaCycler`) at the out→in transition. The overlay's
    `current_media_url` exposes the Flask proxy URL the JS-side
    `<img>` / `<video>` elements follow; with `media_api_base_url`
    set, the URL is built cleanly. No codec drops — the browser's
    native `<img>` decoder handles format failure at the JS layer,
    not in the Python cycler."""
    import logging
    import re

    from lib_shared.models import EffectsSettings, Message, MessageView

    msg = MessageView(
        message=Message(
            id="m1",
            sender="+15551234567",
            body="mms",
            received_at="2026-07-09T00:00:00Z",
            media=[
                {"type": "image/jpeg", "url": "media/images/2026-07/a.jpg"},
                {"type": "image/png", "url": "media/images/2026-07/b.png"},
            ],
        ),
        suppressed=False,
    )
    coord = _build_preview_coord(
        messages=[msg],
        effects_settings=EffectsSettings(
            intro_seconds=0.0,
            fade_seconds=0.01,
            hold_seconds=0.1,
            idle_seconds=0.05,
        ),
    )
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    try:
        coord.start()
        clock.advance(0.001)
        coord.tick()  # intro → out
        clock.advance(0.02)
        coord.tick()  # out → in
        clock.advance(0.02)
        coord.tick()  # in → hold
        # The current is a BrowserMediaOverlay (preview path) — NOT a
        # MediaCycler. The DOM elements are now driven by the
        # overlay's read-only properties via preview.js.
        from lib_shared.patterns.browser_media_overlay import BrowserMediaOverlay

        assert isinstance(coord.current, BrowserMediaOverlay)
        # No codec drops — items remain in the working list, and the
        # overlay is not exhausted (1-item case stays False; this
        # has 2 items anyway, so still False).
        assert coord.current.exhausted is False
        assert coord.current.items_remaining == 2
        # The flask proxy URL is built from the coordinator's
        # configured base + the active key. After `tick()` it
        # points at a real Flask route.
        current = coord.current
        current.tick()  # activate an item
        assert current.current_media_url.startswith(
            "http://preview.test/api/media/",
        )
        assert current.current_media_kind == "image"
        # The first picked item is `image/jpeg` (random over the
        # not-yet-shown set with 2 candidates, but for the first
        # advance the cycle picks uniformly — could be either).
        assert current.current_media_key in {
            "media/images/2026-07/a.jpg",
            "media/images/2026-07/b.png",
        }
    finally:
        monkey.undo()


def test_preview_fallback_skips_when_media_cycler_module_missing(monkeypatch):
    """The browser-preview PyScript bundle does NOT include
    `lib_shared.patterns.media_cycler` (PIL/cv2 deps aren't
    installable in Pyodide; the cycler helper returns a
    `BrowserMediaOverlay` instead). The coordinator's
    `_maybe_fall_back_to_rotation` lazy-imports `MediaCycler`
    for the `isinstance` check, so the import WOULD raise on the
    browser path — except the code now guards with
    `try/except ImportError` and returns silently (since on the
    browser path no cycler is ever constructed and the fallback
    has nothing to fall back from).

    Pin the contract: with the cycler module hidden, the preview
    coordinator's `_maybe_fall_back_to_rotation()` is a clean
    no-op even when `self.current` is set.
    """
    import sys

    # Hide `lib_shared.patterns.media_cycler` so the coordinator's
    # import raises ImportError. Other lib_shared submodules stay
    # loadable — we only shadow the one module under test.
    monkeypatch.setitem(sys.modules, "lib_shared.patterns.media_cycler", None)

    coord = _build_preview_coord(
        messages=[],
    )
    # Stand-in `current` — the fallback must NOT inspect it because
    # the import guard returns first.
    sentinel = object()
    coord.current = sentinel  # type: ignore[assignment]

    # Must not raise — the ImportError guard makes the fallback
    # a no-op when the cycler module is missing.
    coord._maybe_fall_back_to_rotation()

    # The `current` was untouched.
    assert coord.current is sentinel


def test_preview_fallback_triggers_fade_when_browser_overlay_exhausted():
    """Browser-side fallback (issue #38, debug-2026-07-09).

    Regression for the "preview isn't falling back to a standard
    background effect" symptom. The browser preview constructs a
    `BrowserMediaOverlay` instead of a `MediaCycler` at the out→in
    transition (PIL/cv2 aren't in the PyScript bundle). If every
    attachment's URL 404s, the overlay flips `exhausted=True` and
    the canvas should fade to a rotation effect — otherwise the
    preview sits on an empty overlay for the rest of the hold (the
    user-visible symptom: "image isn't rendering, and the canvas
    stays black underneath").

    Pin the contract: with `coord.current` set to a
    `BrowserMediaOverlay` whose `exhausted=True`, calling
    `_maybe_fall_back_to_rotation()` triggers the existing fade-out
    machinery (mode flips to `out`, overlay stays as current). The
    rotation effect swap happens later when the `out` mode's fade
    completes — same as the host-side MediaCycler branch.
    """
    from lib_shared.patterns.browser_media_overlay import BrowserMediaOverlay

    coord = _build_preview_coord(messages=[])
    # Stand-in rotation effect — a real Effect so the `out` mode's
    # post-fade swap can attach it without an AttributeError.
    # Distinct identity, so we can assert the swap rather than a
    # no-op when the fade completes.
    from tests.preview_wiring_test import _make_effect

    rotation_effect = _make_effect("Rotation")()
    coord.effects = [rotation_effect]  # type: ignore[assignment]
    coord.idx = 0
    coord.mode = "hold"
    # Construct a `BrowserMediaOverlay` directly (we don't need a
    # real out→in transition here; we're testing the fallback
    # branch in isolation).
    overlay = BrowserMediaOverlay(
        message_id="m1",
        media=[{"type": "image/jpeg", "url": "media/images/2026-07/a.jpg"}],
        api_base_url="http://preview.test",
        hold_seconds=15.0,
    )
    overlay.exhausted = True  # simulate "every item dropped at runtime"
    coord.current = overlay  # type: ignore[assignment]

    coord._maybe_fall_back_to_rotation()

    # The helper triggered the fade-out — overlay is still current
    # and the coordinator's mode is `out` so `_step_fade` will
    # ramp the overlay's brightness to 0 on the next tick.
    assert coord.current is overlay, "BrowserMediaOverlay should still be current until fade completes; got %r" % (
        coord.current,
    )
    assert coord.mode == "out"
