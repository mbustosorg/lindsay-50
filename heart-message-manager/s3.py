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
from datetime import datetime
from typing import Iterator

from server_time import now_utc_iso, to_utc_datetime
from lib_shared.models import Message
from lib_shared.config_reader import get_config

cfg = get_config()

logger = logging.getLogger(__name__)

# S3 key templates — fill in {year}, {month}, {datetime} at runtime (UTC)
_MESSAGE_KEY_TEMPLATE = "messages/{year}-{month}/msg-{datetime}.json"
_CONFIG_KEY_TEMPLATE = "config/{year}-{month}/cfg-{datetime}.json"

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


def _format_s3_key(key_template: str, dt: datetime) -> str:
    """Fill in {year}, {month}, {datetime} in an S3 key template.

    Args:
        key_template: S3 key with {year}, {month}, {datetime} placeholders.
        dt: UTC datetime object (from to_utc_datetime).
    """
    return key_template.format(
        year=dt.strftime("%Y"),
        month=dt.strftime("%m"),
        datetime=dt.strftime("%Y-%m-%dT%H-%M-%SZ"),
    )


# ---------------------------------------------------------------------------
# Message files
# ---------------------------------------------------------------------------


def log_message(msg: Message) -> None:
    """Write a message to its own S3 file."""
    entry = {
        "id": msg.id,
        "sender": msg.sender,
        "body": msg.body,
        "received_at": msg.received_at,
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
