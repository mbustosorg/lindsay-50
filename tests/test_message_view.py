"""Tests for the `MessageView` wire shape.

The browser preview and the testing page both read `MessageView`
instances through Pyodide's `JsProxy`. Pyodide 0.26 only exposes
attributes set in `__init__` (not @property accessors) as
enumerable keys on the proxy — that's the contract JS code relies
on when it does `item.media`, `item.source`, `item.display_time`,
or `JSON.stringify(item)`.

Issue #38 wired `media` into the testing page's modal but the
field was only nested under `message.media`; the JS-side proxy
saw no top-level `media` and the modal rendered the field in the
wrong place. The fix is to mirror `message.media` as a flat
attribute on `MessageView` itself.
"""

from lib_shared.models import Message, MessageView


def _msg(media=None):
    return Message(
        id="m1",
        sender="+15551234567",
        body="hello",
        received_at="2026-07-09T15:30:00Z",
        media=media if media is not None else [],
    )


def test_message_view_exposes_media_at_top_level():
    """`view.media` works without going through `view.message.media`."""
    msg = _msg(media=[{"type": "image/png", "url": "media/images/2026-07/x.png"}])
    view = MessageView(msg, source="mqtt")
    assert view.media == [{"type": "image/png", "url": "media/images/2026-07/x.png"}]


def test_message_view_media_defaults_to_message_media():
    """Constructor with no explicit media mirrors `message.media`."""
    msg = _msg(media=[{"type": "image/jpeg", "url": "k.jpg"}])
    view = MessageView(msg)
    assert view.media == msg.media


def test_message_view_media_defaults_to_empty_when_message_has_none():
    """An SMS-only message has `media=[]`; view mirrors it."""
    msg = _msg(media=[])
    view = MessageView(msg)
    assert view.media == []


def test_message_view_media_explicit_override_wins():
    """The constructor kwarg lets callers override (defensive)."""
    msg = _msg(media=[{"type": "image/png", "url": "from-msg.png"}])
    override = [{"type": "video/mp4", "url": "override.mp4"}]
    view = MessageView(msg, media=override)
    assert view.media == override


def test_message_view_to_dict_still_includes_media():
    """`to_dict()` continues to expose `media` so Flask/JSON dumps
    don't regress."""
    msg = _msg(media=[{"type": "image/png", "url": "k.png"}])
    view = MessageView(msg, source="rest")
    assert view.to_dict()["media"] == [{"type": "image/png", "url": "k.png"}]


def test_message_view_media_isolated_from_message_mutation():
    """`view.media` is a copy, not a live reference to `message.media`.

    If the inner Message is mutated (e.g. via a fresh wire envelope),
    the existing view's `media` stays put — callers iterating the
    ring buffer don't see a torn snapshot.
    """
    msg = _msg(media=[{"type": "image/png", "url": "old.png"}])
    view = MessageView(msg)
    msg.media.append({"type": "image/png", "url": "new.png"})
    assert view.media == [{"type": "image/png", "url": "old.png"}]
