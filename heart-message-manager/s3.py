"""S3 backup helpers for message logging and config snapshots.

Flask is the source of truth on S3:
  - Every inbound message is written to a per-message file before the TwiML response.
  - On startup, Flask rebuilds SQLite from S3 message files.
  - Every config change triggers a timestamped snapshot to S3; old snapshots are pruned (keep 10).
  - MMS attachments are downloaded from Twilio's Basic-Auth URLs and copied
    here under `media/images/` or `media/videos/` (parallel to the existing
    `messages/` and `config/` prefixes). Twilio's retention is short — even
    successful fetches can 410 a few days later (design D2) — so copying is
    mandatory, not optional.

S3 key templates (hardcoded, not configurable):
  messages:  messages/{year}-{month}/msg-{datetime}.json
  config:    config/{year}-{month}/cfg-{datetime}.json
  media/images: media/images/{year}-{month}/media-{datetime}.{ext}
  media/videos: media/videos/{year}-{month}/media-{datetime}.{ext}

Internal timestamps are ISO 8601 strings. S3 keys use UTC. Display formatting
uses the configured local timezone from settings.toml (default: US/Pacific).
"""

import json
import logging
import re
import boto3
from datetime import datetime, timezone
from typing import Iterator
from urllib.parse import urlparse

import requests  # Twilio MediaUrl downloads (Basic Auth over HTTPS)

from server_time import now_utc_iso, to_utc_datetime
from lib_shared.models import Message
from lib_shared.config_reader import get_config

cfg = get_config()

logger = logging.getLogger(__name__)

# S3 key templates — fill in {year}, {month}, {datetime} at runtime (UTC)
_MESSAGE_KEY_TEMPLATE = "messages/{year}-{month}/msg-{datetime}.json"
_CONFIG_KEY_TEMPLATE = "config/{year}-{month}/cfg-{datetime}.json"

# Media key templates — same date partitioning as messages/config. The
# extra `{ext}` slot is the source-file extension, lowercased and stripped
# of any URL-unsafe chars (see `_safe_ext` for the normalizer).
_MEDIA_KEY_TEMPLATE_IMAGES = "media/images/{year}-{month}/media-{datetime}{ext}"
_MEDIA_KEY_TEMPLATE_VIDEOS = "media/videos/{year}-{month}/media-{datetime}{ext}"

# Prefixes that hold MMS media (separate namespace from message-body files).
# Listed here so callers that scan all S3 prefixes (e.g. `rebuild_from_s3`)
# can skip media without hardcoding the namespace shape — see
# `sqlite.rebuild_from_s3`'s skip filter.
MEDIA_KEY_PREFIXES = ("media/images/", "media/videos/")

# Default Twilio MediaUrl fetch timeout. A real-world fetch can stall on a
# slow edge node; the operator can't afford to wait minutes per attachment
# inside the async thread (the Pi is already rendering the message text).
# The thread is non-blocking at the HTTP request layer — `log_media` runs in
# a worker pool slot, not the request handler — but a hung request can
# starve a parallel uploader of its slot.
_MEDIA_FETCH_TIMEOUT_S = 10.0

# MIME → (subdir, extension). Kept permissive: anything not matching
# `image/*` or `video/*` (e.g., audio/*) lands on the WARNING path
# in `log_media` and is dropped. The extension is the canonical one
# for the MIME family — `.jpg` for `image/jpeg`, `.mp4` for
# `video/mp4`, etc.
_MIME_EXT_TABLE = {
    "image/jpeg": (".jpg",),
    "image/jpg": (".jpg",),
    "image/png": (".png",),
    "image/gif": (".gif",),
    "image/webp": (".webp",),
    "video/mp4": (".mp4",),
    "video/quicktime": (".mov",),
    "video/x-msvideo": (".avi",),
    "video/x-matroska": (".mkv",),
    "video/webm": (".webm",),
    "video/gif": (".gif",),
}

# ---------------------------------------------------------------------------
# S3 client (lazily created on first call)
# ---------------------------------------------------------------------------


def _s3_client():
    """Return a cached boto3 S3 client (credentials from config)."""
    endpoint = cfg.if_exists("AWS_S3_ENDPOINT_URL")
    return boto3.client(
        "s3",
        aws_access_key_id=cfg.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=cfg.AWS_SECRET_ACCESS_KEY,
        endpoint_url=endpoint,
        region_name=cfg.AWS_S3_REGION,
    )


def _s3_bucket() -> str:
    """Return the S3 bucket name from config."""
    return cfg.AWS_S3_BUCKET


# ---------------------------------------------------------------------------
# Internal: ISO strings, with UTC conversion only for S3 key formatting
# ---------------------------------------------------------------------------


def _format_s3_key(key_template: str, dt: datetime, **extra: str) -> str:
    """Fill in {year}, {month}, {datetime} (and any extra placeholders) in an S3 key template.

    Args:
        key_template: S3 key with ``{year}``, ``{month}``, ``{datetime}``
            placeholders (and optionally any extras via ``**extra``).
        dt: UTC datetime object (from to_utc_datetime).
        **extra: Optional additional template substitutions — e.g. the
            ``{ext}`` slot in the media templates.
    """
    return key_template.format(
        year=dt.strftime("%Y"),
        month=dt.strftime("%m"),
        datetime=dt.strftime("%Y-%m-%dT%H-%M-%SZ"),
        **extra,
    )


# ---------------------------------------------------------------------------
# Message files
# ---------------------------------------------------------------------------


def log_message(msg: Message) -> None:
    """Write a message to its own S3 file.

    The wire shape includes the `media` list verbatim when present
    (S3 keys, not URLs — design D2). SMS-only messages have
    ``media == []`` and the field still serializes to `[]` so the
    on-S3 shape is always 5-key (id, sender, body, received_at, media).
    """
    entry = {
        "id": msg.id,
        "sender": msg.sender,
        "body": msg.body,
        "received_at": msg.received_at,
        "media": msg.media,
    }
    key = _format_s3_key(_MESSAGE_KEY_TEMPLATE, to_utc_datetime(msg.received_at))

    _s3_client().put_object(
        Bucket=_s3_bucket(),
        Key=key,
        Body=json.dumps(entry, separators=(",", ":")).encode(),
        ContentType="application/json",
    )
    logger.info("Logged message to s3://%s/%s", _s3_bucket(), key)


def load_messages_from_s3() -> Iterator[Message]:
    """Yield all messages from S3 message files.

    Scans all message prefixes under messages/ and yields each parsed file.
    Used by Flask on startup to rebuild SQLite.

    The `media` field is optional on disk (4-field legacy payloads still
    exist pre-MMS). `Message.from_dict` defaults it to `[]` so legacy
    messages round-trip without error.
    """
    bucket = _s3_bucket()
    client = _s3_client()

    paginator = client.get_paginator("list_objects_v2")
    try:
        pages = paginator.paginate(Bucket=bucket, Prefix="messages/")
    except client.exceptions.NoSuchBucket:
        return

    for page in pages:
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".json"):
                continue
            try:
                content = client.get_object(Bucket=bucket, Key=key)["Body"].read().decode()
                d = json.loads(content)
                yield Message(
                    id=d["id"],
                    sender=d["sender"] or d["sender_name"],
                    body=d["body"],
                    received_at=d["received_at"],
                    media=d.get("media", []),
                )
            except (KeyError, json.JSONDecodeError) as e:
                logger.warning("Skipping malformed S3 message file %s: %s", key, e)


# ---------------------------------------------------------------------------
# Config snapshots
# ---------------------------------------------------------------------------


def save_config_snapshot(config_dict: dict) -> None:
    """Save a timestamped config snapshot to S3 and prune old snapshots (keep 10)."""
    dt_iso = now_utc_iso()
    bucket = _s3_bucket()
    key = _format_s3_key(_CONFIG_KEY_TEMPLATE, to_utc_datetime(dt_iso))

    _s3_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(config_dict, separators=(",", ":")).encode(),
        ContentType="application/json",
    )
    logger.info("Saved config snapshot to s3://%s/%s", bucket, key)

    _prune_config_snapshots(bucket)


def _prune_config_snapshots(bucket: str) -> None:
    """Delete oldest config snapshots, keeping the 10 most recent."""
    paginator = _s3_client().get_paginator("list_objects_v2")
    response = paginator.paginate(Bucket=bucket, Prefix="config/")
    keys = [obj["Key"] for page in response for obj in page.get("Contents", [])]

    if len(keys) <= 10:
        return

    keys.sort()
    for old_key in keys[:-10]:
        _s3_client().delete_object(Bucket=bucket, Key=old_key)
        logger.info("Pruned old config snapshot: s3://%s/%s", bucket, old_key)


def load_latest_config() -> dict | None:
    """Load the most recent config snapshot from S3."""
    bucket = _s3_bucket()
    paginator = _s3_client().get_paginator("list_objects_v2")

    response = paginator.paginate(Bucket=bucket, Prefix="config/")
    keys = [obj["Key"] for page in response for obj in page.get("Contents", [])]

    if not keys:
        return None

    latest_key = sorted(keys)[-1]
    content = _s3_client().get_object(Bucket=bucket, Key=latest_key)["Body"].read().decode()
    return json.loads(content)


# ---------------------------------------------------------------------------
# Media storage (MMS attachments — Twilio MediaUrl → our S3)
#
# The async MMS webhook pipeline (see heart-message-manager/main.py:
# `_process_inbound_media_async`) calls `log_media` per attachment. The
# background thread's only responsibility is to copy Twilio's bytes into OUR
# bucket before publishing the `Message` envelope over MQTT — Twilio's
# retention window is short and undocumented, and reading past it returns
# 410 GONE (R1).
# ---------------------------------------------------------------------------


# Regex: anything that's not alphanumeric / underscore / dash / dot. Source
# extensions from Twilio are already well-formed, but a defensive strip
# guards against accidental `path/to/file.jpg` strings or `../` traversal
# attempts being smuggled through the filename.
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9_.-]")


def _safe_ext(content_type: str, fallback: str = "") -> str:
    """Return a clean `.ext` string for the given content-type, including
    the leading dot, or `fallback` (also expected to include the dot) on
    unknown types. Lowercases the result so `/media/.../key.MP4` and
    `/media/.../key.mp4` are indistinguishable on the wire.
    """
    ext = _MIME_EXT_TABLE.get(content_type.lower())
    if ext is None:
        return fallback
    return ext[0].lower()


def _classify_mime(content_type: str) -> str | None:
    """Return "images" / "videos" / None for a MIME type.

    Returns None for unknown types so `log_media` can log a WARNING and
    drop the item rather than write it under a wrong prefix (matches the
    spec scenario "Unknown content type").
    """
    if not content_type:
        return None
    c = content_type.lower()
    if c == "image/gif":
        # GIFs are images regardless of multi-frame semantics (spec).
        return "images"
    if c.startswith("image/"):
        return "images"
    if c.startswith("video/"):
        return "videos"
    return None


def _media_key(content_type: str, dt: datetime, source_url: str = "") -> str | None:
    """Resolve the S3 key for a new media upload.

    Picks `media/images/...` or `media/videos/...` from the content type,
    applies the date partitioning from `_format_s3_key`, and synthesizes an
    extension (with a sensible fallback pulled from the source URL's
    path if the MIME table doesn't have one).
    """
    sub = _classify_mime(content_type)
    if sub == "images":
        tmpl = _MEDIA_KEY_TEMPLATE_IMAGES
    elif sub == "videos":
        tmpl = _MEDIA_KEY_TEMPLATE_VIDEOS
    else:
        # Should be unreachable — `log_media` drops unknown MIME types
        # before they reach this helper. Defensive log + None signaling
        # so a regression here surfaces immediately rather than silently
        # producing a wrong prefix.
        logger.warning("s3._media_key: unclassified content_type=%r", content_type)
        return None  # type: ignore[return-value]
    ext = _safe_ext(content_type)
    if not ext:
        # Last-ditch fallback: pull a dot-suffix off the source URL path
        # (e.g. ".jpg" from "https://api.twilio.com/.../ME19...jpg").
        try:
            url_path = urlparse(source_url).path
            dot = url_path.rfind(".")
            if dot > 0 and dot < len(url_path) - 1:
                candidate = url_path[dot:].lower()
                if _UNSAFE_CHARS.search(candidate) is None and len(candidate) <= 8:
                    ext = candidate
        except Exception:
            pass
    if not ext:
        ext = ".bin"  # genuinely unknown — should be rare; visible to operator
    return _format_s3_key(tmpl, dt, ext=ext)


def log_media(content_type: str, source_url: str) -> str | None:
    """Download a Twilio MediaUrl and copy the bytes to OUR S3.

    Returns the S3 key (``media/images/.../...jpg`` or
    ``media/videos/.../...mp4``) on success, or None on any failure
    (download error, S3 put error, unsupported MIME). The webhook
    handler's async thread drops None-returning items from the wire's
    `media` list (with WARNING) so a single bad attachment never
    blocks the whole MMS.

    Auth: Twilio `MediaUrl*` endpoints accept HTTP Basic with
    ``AccountSID:AuthToken``. We use the same `TWILIO_AUTH_TOKEN` env
    var the webhook signature validator uses (design D8). For
    AccountSID we fall back to the FIRST valid TWILIO_ACCOUNT_SID env
    var the operator might have set; we don't strictly need the
    account SID for Basic Auth on MediaUrl fetches (Twilio will
    authenticate the token against the URL's owner), but pairing
    them matches Twilio's docs.

    Args:
        content_type: MIME type from Twilio's `MediaContentType0..N`
            form field, e.g. ``"image/jpeg"``.
        source_url: Twilio `MediaUrl0..N`, e.g.
            ``https://api.twilio.com/.../Messages/MM.../Media/ME...``.
    """
    sub = _classify_mime(content_type)
    if sub is None:
        logger.warning(
            "s3.log_media: dropping unsupported MIME type content_type=%r url=%s",
            content_type,
            source_url,
        )
        return None
    if not source_url:
        logger.warning("s3.log_media: empty source_url for content_type=%r", content_type)
        return None

    # Pull bytes via Twilio Basic Auth over HTTPS.
    twilio_token = cfg.if_exists("TWILIO_AUTH_TOKEN") or ""
    twilio_sid = cfg.if_exists("TWILIO_ACCOUNT_SID") or ""
    auth = (twilio_sid, twilio_token) if twilio_sid else None
    try:
        resp = requests.get(source_url, auth=auth, timeout=_MEDIA_FETCH_TIMEOUT_S)
    except Exception as exc:
        logger.warning(
            "s3.log_media: Twilio fetch failed content_type=%r url=%s err=%s",
            content_type,
            source_url,
            exc,
        )
        return None
    if resp.status_code != 200:
        logger.warning(
            "s3.log_media: Twilio fetch returned %s content_type=%r url=%s",
            resp.status_code,
            content_type,
            source_url,
        )
        return None
    body = resp.content
    if not body:
        logger.warning(
            "s3.log_media: Twilio fetch returned empty body content_type=%r url=%s",
            content_type,
            source_url,
        )
        return None

    # Build the S3 key.
    dt = datetime.now(timezone.utc)
    key = _media_key(content_type, dt, source_url=source_url)
    if not key:
        # Defensive — `_media_key` already returns None for unclassified types.
        return None

    # boto3 put. Errors here drop the item from the wire's media list (the
    # caller treats None as "couldn't process this attachment") — the text
    # body still publishes, the operator still sees a graceful text-only
    # background if the S3 put blows up.
    try:
        _s3_client().put_object(
            Bucket=_s3_bucket(),
            Key=key,
            Body=body,
            ContentType=content_type,
        )
    except Exception as exc:
        logger.warning(
            "s3.log_media: S3 put failed content_type=%r key=%s err=%s",
            content_type,
            key,
            exc,
        )
        return None

    logger.info(
        "Logged media to s3://%s/%s content_type=%r bytes=%d",
        _s3_bucket(),
        key,
        content_type,
        len(body),
    )
    return key


def signed_media_url(s3_key: str, expires_in: int = 3600) -> str | None:
    """Return a freshly-signed S3 URL for `GET /api/media/<key>` callers.

    Called from the Flask 302 endpoint — every fetch regenerates a fresh
    signed URL with a 1-hour TTL by default. The Pi/browser never sees
    AWS creds; it follows the 302 to the signed URL and downloads the
    bytes from S3 directly. TTL is invisible to the wire shape
    (`Message.media[*].url` is a logical S3 key, design D2).

    Args:
        s3_key: The logical key on the bucket, e.g.
            ``media/images/2026-07/media-2026-07-09T15-30-00Z.jpg``.
        expires_in: TTL seconds (default 3600).

    Returns:
        The signed URL string on success, or None on boto3 failure.
    """
    try:
        return _s3_client().generate_presigned_url(
            "get_object",
            Params={"Bucket": _s3_bucket(), "Key": s3_key},
            ExpiresIn=expires_in,
        )
    except Exception as exc:
        logger.warning("s3.signed_media_url: signing failed key=%s err=%s", s3_key, exc)
        return None
