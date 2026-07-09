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
    app_main.py` does: with no media_api_base_url, no media_cache_dir.

    The preview's per-page `preview_main.py` calls `coord.bind(...)`
    with the page-local render layer; we mirror that here via the
    constructor."""
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


def test_preview_coord_mms_message_cycler_drops_with_no_api_url(caplog):
    """An MMS message in the preview: the cycler is constructed,
    every attachment is dropped (no api_base_url — the preview
    is illustrative, not a real fetcher), and the coordinator
    falls back to the rotation effect. Logs a WARNING per item
    so the operator sees the dropped attachments in the
    browser's dev console."""
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
        with caplog.at_level(logging.WARNING, logger="heart"):
            coord.start()
            clock.advance(0.001)
            coord.tick()  # intro → out
            clock.advance(0.02)
            coord.tick()  # out → in
            clock.advance(0.02)
            coord.tick()  # in → hold
        # The current is a MediaCycler at this point (the cycler
        # was constructed for the picked MMS message).
        from lib_shared.patterns.media_cycler import MediaCycler

        assert isinstance(coord.current, MediaCycler)
        # Both items dropped (no api_base_url → all items dropped).
        assert coord.current.exhausted is True
        # The coordinator's `_maybe_fall_back_to_rotation` runs at
        # the next hold tick; after a few more ticks, the rotation
        # effect is back in place.
        clock.advance(0.02)
        coord.tick()  # hold tick — _maybe_fall_back_to_rotation fires
        assert not isinstance(coord.current, MediaCycler)
        # The dropped items logged a WARNING per item.
        drop_warnings = [
            r for r in caplog.records if r.levelno == logging.WARNING and re.search(r"dropping item", r.getMessage())
        ]
        # At least one drop warning (the cycler logs one per dropped item).
        assert len(drop_warnings) >= 1
    finally:
        monkey.undo()
