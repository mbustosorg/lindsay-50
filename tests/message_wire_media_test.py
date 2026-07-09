"""Round-trip tests for the `Message.media` wire field (issue #38 / openspec
`add-image-and-video-support` capability `mms-media-support`).

Pins the wire shape: SMS-only messages carry `media == []` (4-field
back-compat preserved); MMS messages round-trip the same
`{"type": str, "url": str}` list they were constructed with. The `type`
string is preserved verbatim (no MIME coercion); the `url` is the S3 key
under our bucket (Twilio MediaUrls are NEVER stored on the wire —
design D2).

Also exercises `MessageView.to_dict()` which passes the media list
through verbatim (no filter or shape change at the view layer — D6).
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from lib_shared.models import Message, MessageView


# ---------------------------------------------------------------------------
# Message round-trip
# ---------------------------------------------------------------------------


def test_to_dict_includes_media_field():
    """Even an empty-MMS Message emits a `media` key on the wire (additive)."""
    msg = Message(id="m1", sender="+15551234567", body="hi", received_at="2026-07-09T15:30:00Z")
    d = msg.to_dict()
    assert "media" in d
    assert d["media"] == []


def test_from_dict_4_field_legacy_payload_defaults_media_to_empty():
    """Pre-MMS messages (no `media` key) round-trip with `media == []`."""
    legacy = {
        "id": "m1",
        "sender": "+15551234567",
        "body": "hi",
        "received_at": "2026-07-09T15:30:00Z",
    }
    msg = Message.from_dict(legacy)
    assert msg.media == []
    # And the to_dict emits it as [] (additive, not missing)
    assert msg.to_dict()["media"] == []


def test_from_dict_explicit_empty_media_round_trips():
    """An explicitly-empty `media: []` survives from_dict → to_dict."""
    d = {
        "id": "m1",
        "sender": "+15551234567",
        "body": "hi",
        "received_at": "2026-07-09T15:30:00Z",
        "media": [],
    }
    msg = Message.from_dict(d)
    assert msg.media == []
    assert msg.to_dict()["media"] == []


def test_round_trip_with_two_media_items_preserves_type_and_url():
    """`{"type": str, "url": str}` entries round-trip with exact-string fidelity."""
    items = [
        {"type": "image/jpeg", "url": "media/images/2026-07/media-2026-07-09T15-30-00Z.jpg"},
        {"type": "video/mp4", "url": "media/videos/2026-07/media-2026-07-09T15-31-00Z.mp4"},
    ]
    msg = Message(
        id="m1",
        sender="+15551234567",
        body="check this out",
        received_at="2026-07-09T15:30:00Z",
        media=items,
    )
    # `msg.media` is a defensive copy — mutating the source list does not
    # leak into the message.
    items.append({"type": "image/png", "url": "later/added.png"})
    assert len(msg.media) == 2

    d = msg.to_dict()
    assert d["media"] == [
        {"type": "image/jpeg", "url": "media/images/2026-07/media-2026-07-09T15-30-00Z.jpg"},
        {"type": "video/mp4", "url": "media/videos/2026-07/media-2026-07-09T15-31-00Z.mp4"},
    ]

    # from_dict → to_dict is a fixed point
    msg2 = Message.from_dict(d)
    assert msg2.media == d["media"]
    assert msg2.to_dict() == d


def test_round_trip_gif_mime_kept_under_media_url_s3_key_format():
    """GIF attachments use MIME `image/gif`; the S3 key on `url` is the
    canonical `media/images/{YYYY-MM}/media-{ISO}.gif` shape (not Twilio's
    MediaUrl* path)."""
    msg = Message(
        id="m1",
        sender="+15551234567",
        body="look at this loop",
        received_at="2026-07-09T15:30:00Z",
        media=[{"type": "image/gif", "url": "media/images/2026-07/media-2026-07-09T15-30-00Z.gif"}],
    )
    d = msg.to_dict()
    assert d["media"][0]["type"] == "image/gif"  # not coerced / not normalized
    assert d["media"][0]["url"].startswith("media/images/")
    assert d["media"][0]["url"].endswith(".gif")


def test_round_trip_preserves_string_types_no_coercion():
    """`type` and `url` are strings, not coerced into anything else."""
    msg = Message.from_dict(
        {
            "id": "m1",
            "sender": "+15551234567",
            "body": "x",
            "received_at": "2026-07-09T15:30:00Z",
            "media": [{"type": "image/jpeg", "url": "k.jpg"}],
        }
    )
    entry = msg.media[0]
    assert isinstance(entry["type"], str)
    assert isinstance(entry["url"], str)
    assert entry == {"type": "image/jpeg", "url": "k.jpg"}


def test_constructor_default_media_is_empty_list():
    """Default constructor (no `media` kwarg) yields an empty list, not None."""
    msg = Message(id="m1", sender="+15551234567", body="hi", received_at="2026-07-09T15:30:00Z")
    assert msg.media == []
    assert isinstance(msg.media, list)


def test_constructor_with_none_media_yields_empty_list():
    """`media=None` (the same sentinel as not passing it) is normalized to []."""
    msg = Message(
        id="m1",
        sender="+15551234567",
        body="hi",
        received_at="2026-07-09T15:30:00Z",
        media=None,
    )
    assert msg.media == []


def test_media_list_is_defensive_copy_at_construction():
    """Mutating the source list after construction does not bleed into msg.media."""
    src = [{"type": "image/jpeg", "url": "a.jpg"}]
    msg = Message(id="m1", sender="+15551234567", body="hi", received_at="2026-07-09T15:30:00Z", media=src)
    src.append({"type": "image/png", "url": "b.png"})
    assert len(msg.media) == 1


# ---------------------------------------------------------------------------
# MessageView carries media through verbatim
# ---------------------------------------------------------------------------


def test_message_view_to_dict_includes_media():
    """MessageView.to_dict() surfaces the message's media list verbatim
    (no filter, no shape change — design D6)."""
    msg = Message(
        id="m1",
        sender="+15551234567",
        body="hi",
        received_at="2026-07-09T15:30:00Z",
        media=[{"type": "image/png", "url": "media/images/2026-07/k.png"}],
    )
    view = MessageView(message=msg, source="rest")
    d = view.to_dict()
    assert "media" in d
    assert d["media"] == [{"type": "image/png", "url": "media/images/2026-07/k.png"}]


def test_message_view_no_media_has_empty_list_on_wire():
    """Even when the message has no media, the view's to_dict emits `media: []`."""
    msg = Message(id="m1", sender="+15551234567", body="hi", received_at="2026-07-09T15:30:00Z")
    view = MessageView(message=msg, source="rest")
    d = view.to_dict()
    assert d["media"] == []
