"""Tests for the malformed-message filter at MessageManager ingest sites.

`_coerce_message_dict` is the single source of truth for "is this
wire/cache dict a valid Message?". The three ingest paths —
cache hydrate, MQTT receive, REST seed — all consult it before
constructing a Message, so an entry with empty/missing `id`,
`sender`, `body`, or `received_at` never reaches the rotation
buffer.

Without the filter, a stored row with `"id": ""` from an older
S3/IndexedDB cache used to coerce to an empty-string id and sit
at `entries[0]` of the buffer. `get_display_message` would then
pick it, and the preview showed:

  [preview-modal] picked message: id=(none) body= media_count=0

…with no scroller text. The filter makes that path unreachable.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from lib_shared.message_manager import _coerce_message_dict  # noqa: E402


def _valid_item() -> dict:
    return {
        "id": "m1",
        "sender": "+15551234567",
        "body": "hello",
        "received_at": "2026-07-10T00:00:00Z",
    }


def test_valid_item_returns_message():
    """A well-formed dict builds a Message with all fields copied through."""
    msg = _coerce_message_dict(_valid_item())
    assert msg is not None
    assert msg.id == "m1"
    assert msg.sender == "+15551234567"
    assert msg.body == "hello"
    assert msg.received_at == "2026-07-10T00:00:00Z"


def test_valid_item_with_media_returns_message_with_media():
    """An MMS dict with a media list carries through."""
    item = _valid_item()
    item["media"] = [{"type": "image/jpeg", "url": "media/images/2026-07/a.jpg"}]
    msg = _coerce_message_dict(item)
    assert msg is not None
    assert len(msg.media) == 1
    assert msg.media[0]["type"] == "image/jpeg"


def test_non_dict_returns_none():
    """Anything that isn't a dict (string, list, None, int) returns None."""
    for item in ("not a dict", [1, 2], None, 42, 3.14):
        assert _coerce_message_dict(item) is None, f"should reject {item!r}"


def test_empty_id_rejected():
    """The diagnosed symptom: empty id (the field that triggered the
    picked message: id=(none) trace). Returns None rather than
    silently coercing to Message(id='', ...)."""
    item = _valid_item()
    item["id"] = ""
    assert _coerce_message_dict(item) is None


def test_missing_id_rejected():
    """A dict with no `id` key at all returns None."""
    item = _valid_item()
    del item["id"]
    assert _coerce_message_dict(item) is None


def test_empty_body_no_media_rejected():
    """Empty body AND no media is malformed — nothing to render.

    Twilio produces empty body ONLY for MMS-only rows (photo/video
    with no caption), never for plain SMS. A row with both empty
    body AND no media can't have come from Twilio's webhook; it's
    a corrupt cache row and should stay rejected."""
    item = _valid_item()
    item["body"] = ""
    assert _coerce_message_dict(item) is None


def test_empty_body_with_empty_media_list_rejected():
    """Empty body AND explicit `media: []` is the same as no media
    field at all — no content to render, reject it."""
    item = _valid_item()
    item["body"] = ""
    item["media"] = []
    assert _coerce_message_dict(item) is None


def test_empty_body_with_media_accepted():
    """Empty body AND a non-empty `media` list is a legitimate
    photo-only or video-only MMS — Twilio sends `Body=""` when
    `NumMedia>0` and there's no caption. The user-reported symptom
    was exactly this case: rows like `body="", media=[{image}]`
    silently dropped from the rotation buffer."""
    item = _valid_item()
    item["body"] = ""
    item["media"] = [{"type": "image/jpeg", "url": "media/images/2026-07/a.jpg"}]
    msg = _coerce_message_dict(item)
    assert msg is not None
    assert msg.body == ""
    assert len(msg.media) == 1
    assert msg.media[0]["type"] == "image/jpeg"


def test_empty_body_with_none_media_rejected():
    """`media=None` is treated as no media — defaults to `[]`,
    then the empty-body+empty-media rule fires."""
    item = _valid_item()
    item["body"] = ""
    item["media"] = None
    assert _coerce_message_dict(item) is None


def test_user_reported_mms_photo_only_row_accepted():
    """Regression: the exact row shape from the user's heroku DB
    that was getting dropped pre-fix. If this fails, the validator
    is back to over-rejecting empty-body MMS rows."""
    item = {
        "id": "a7edac79-ae59-44e1-8d28-5bec5f548fa2",
        "sender": "+14152985015",
        "body": "",
        "received_at": "2026-07-10T05:16:44Z",
        "media": [{"type": "image/jpeg", "url": "media/images/2026-07/a.jpg"}],
    }
    msg = _coerce_message_dict(item)
    assert msg is not None
    assert msg.id == "a7edac79-ae59-44e1-8d28-5bec5f548fa2"
    assert msg.body == ""
    assert len(msg.media) == 1


def test_user_reported_mms_video_only_row_accepted():
    """Regression: same wire shape but `video/3gpp` instead of
    `image/jpeg`. The rule is media-type-agnostic — any non-empty
    media list enables empty body."""
    item = {
        "id": "2e384b3a-a3c8-4a5d-bda4-64da7a0440d7",
        "sender": "+14152985015",
        "body": "",
        "received_at": "2026-07-10T04:49:38Z",
        "media": [{"type": "video/3gpp", "url": "media/videos/2026-07/b.bin"}],
    }
    msg = _coerce_message_dict(item)
    assert msg is not None
    assert msg.id == "2e384b3a-a3c8-4a5d-bda4-64da7a0440d7"
    assert len(msg.media) == 1
    assert msg.media[0]["type"] == "video/3gpp"


def test_empty_sender_rejected():
    """Empty sender is malformed — every real SMS has a phone number."""
    item = _valid_item()
    item["sender"] = ""
    assert _coerce_message_dict(item) is None


def test_empty_received_at_rejected():
    """Empty received_at is malformed — every real message has a timestamp."""
    item = _valid_item()
    item["received_at"] = ""
    assert _coerce_message_dict(item) is None


def test_wrong_type_id_rejected():
    """Non-string id (e.g. int, None) returns None."""
    for bad in (None, 0, 42, [], {}):
        item = _valid_item()
        item["id"] = bad  # type: ignore[assignment]
        assert _coerce_message_dict(item) is None, f"should reject id={bad!r}"


def test_media_not_a_list_rejected():
    """`media` must be a list when present — a scalar or dict is malformed."""
    for bad_media in ("just a string", 42, {"type": "x"}, True):
        item = _valid_item()
        item["media"] = bad_media  # type: ignore[assignment]
        assert _coerce_message_dict(item) is None, f"should reject media={bad_media!r}"


def test_media_filters_non_dict_entries():
    """An MMS dict with the right shape plus a stray non-dict entry
    in `media` builds a Message but drops the bad entry rather than
    poisoning the list."""
    item = _valid_item()
    item["media"] = [
        {"type": "image/jpeg", "url": "a.jpg"},
        "stray-string",
        {"type": "video/mp4", "url": "b.mp4"},
        42,
    ]
    msg = _coerce_message_dict(item)
    assert msg is not None
    assert len(msg.media) == 2
    assert all(isinstance(m, dict) for m in msg.media)


def test_media_key_absent_defaults_to_empty_list():
    """Omitting `media` (the SMS-only case) returns a Message with
    `media == []` — the wire shape for SMS-only messages."""
    item = _valid_item()
    msg = _coerce_message_dict(item)
    assert msg is not None
    assert msg.media == []


def test_media_key_none_defaults_to_empty_list():
    """`media=None` is treated the same as `media` missing — defaults to `[]`."""
    item = _valid_item()
    item["media"] = None
    msg = _coerce_message_dict(item)
    assert msg is not None
    assert msg.media == []
