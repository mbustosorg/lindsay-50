"""S3 backup helpers for message logging and config snapshots.

Flask is the source of truth on S3:
  - Every inbound message is written to a per-message file before the TwiML response.
  - On startup, Flask rebuilds SQLite from S3 message files.
  - Every config change triggers a timestamped snapshot to S3; old snapshots are pruned (keep 10).

S3 key templates (hardcoded, not configurable):
  messages:  messages/{year}-{month}/msg-{datetime}.json
  config:    config/{year}-{month}/cfg-{datetime}.json

Internal timestamps are ISO 8601 strings. S3 keys use UTC. Display formatting
uses the configured local timezone from settings.toml (default: US/Pacific).
"""

import json
import logging
import boto3
from datetime import datetime, timezone
from typing import Iterator, Optional
import pytz

from lib_shared.config import cfg
from lib_shared.models import Message

logger = logging.getLogger(__name__)

# S3 key templates — fill in {year}, {month}, {datetime} at runtime (UTC)
_MESSAGE_KEY_TEMPLATE = "messages/{year}-{month}/msg-{datetime}.json"
_CONFIG_KEY_TEMPLATE = "config/{year}-{month}/cfg-{datetime}.json"

# ---------------------------------------------------------------------------
# S3 client (lazily created on first call)
# ---------------------------------------------------------------------------

def _s3_client():
    return boto3.client(
        "s3",
        aws_access_key_id=cfg.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=cfg.get("AWS_SECRET_ACCESS_KEY"),
        endpoint_url=cfg.get("AWS_S3_ENDPOINT_URL"),
        region_name=cfg.get("AWS_S3_REGION", "us-east-1"),
    )


def _s3_bucket() -> str:
    return cfg.get("AWS_S3_BUCKET", "")


# ---------------------------------------------------------------------------
# Internal: ISO strings, with UTC conversion only for S3 key formatting
# ---------------------------------------------------------------------------

def _to_utc_datetime(dt_iso: str) -> datetime:
    """Parse an ISO 8601 timestamp and return it as a UTC datetime."""
    dt = datetime.fromisoformat(dt_iso.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc)


def _format_s3_key(key_template: str, dt_iso: str) -> str:
    """Build an S3 key from an ISO timestamp string.

    Converts to UTC for the key format so S3 objects sort chronologically.
    """
    dt_utc = _to_utc_datetime(dt_iso)
    return key_template.format(
        year=dt_utc.strftime("%Y"),
        month=dt_utc.strftime("%m"),
        datetime=dt_utc.strftime("%Y-%m-%dT%H-%M-%SZ"),
    )


# ---------------------------------------------------------------------------
# Display formatting (local timezone)
# ---------------------------------------------------------------------------

def format_timestamp_display(dt_iso: str, tz_name: Optional[str] = None) -> str:
    """Format an ISO 8601 timestamp for display in the local timezone.

    Args:
        dt_iso: ISO 8601 timestamp (UTC with Z or with offset).
        tz_name: IANA timezone name (e.g. "US/Pacific"). Defaults to configured TIMEZONE.

    Returns:
        Formatted string in local timezone, e.g. "2026-05-10 14:32:01 PST".
    """
    if tz_name is None:
        tz_name = "US/Pacific"
    tz = pytz.timezone(tz_name)
    dt_utc = _to_utc_datetime(dt_iso)
    dt_local = dt_utc.astimezone(tz)
    return dt_local.strftime("%Y-%m-%d %H:%M:%S %Z")


# ---------------------------------------------------------------------------
# Message files
# ---------------------------------------------------------------------------

def _message_key(dt_iso: str) -> str:
    """Build the S3 key for a message file."""
    return _format_s3_key(_MESSAGE_KEY_TEMPLATE, dt_iso)


def log_message(msg: Message) -> None:
    """Write a message to its own S3 file."""
    entry = {
        "id": msg.id,
        "sender": msg.sender,
        "body": msg.body,
        "received_at": msg.received_at,
    }
    key = _message_key(msg.received_at)

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
                )
            except (KeyError, json.JSONDecodeError) as e:
                logger.warning("Skipping malformed S3 message file %s: %s", key, e)


# ---------------------------------------------------------------------------
# Config snapshots
# ---------------------------------------------------------------------------

def _config_key(dt_iso: str) -> str:
    """Build the S3 key for a config snapshot file."""
    return _format_s3_key(_CONFIG_KEY_TEMPLATE, dt_iso)


def save_config_snapshot(config_dict: dict) -> None:
    """Save a timestamped config snapshot to S3 and prune old snapshots (keep 10)."""
    dt_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    bucket = _s3_bucket()
    key = _config_key(dt_iso)

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


def load_latest_config() -> Optional[dict]:
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
