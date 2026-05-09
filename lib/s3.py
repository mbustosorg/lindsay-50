"""S3 backup helpers for message logging and config snapshots.

Flask is the source of truth on S3:
  - Every inbound message is appended to the S3 message log before the TwiML response.
  - On startup, Flask rebuilds SQLite from the S3 message log.
  - Every config change triggers a timestamped snapshot to S3; old snapshots are pruned (keep 10).
"""

import json
import logging
import boto3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from .models import Message

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# S3 client (lazily created on first call)
# ---------------------------------------------------------------------------

def _s3_client():
    return boto3.client("s3")


# ---------------------------------------------------------------------------
# Config (loaded from settings.toml)
# ---------------------------------------------------------------------------

def _load_s3_config() -> dict:
    """Load S3 configuration from settings.toml."""
    import tomllib
    settings_path = Path(__file__).parent.parent / "heart-sms-receiver" / "settings.toml"
    if not settings_path.exists():
        return {}
    with open(settings_path, "rb") as f:
        return tomllib.load(f)


# ---------------------------------------------------------------------------
# Message log (JSONL, one message per line)
# ---------------------------------------------------------------------------

def _message_log_key() -> str:
    cfg = _load_s3_config()
    return cfg.get("S3_MESSAGE_LOG_KEY", "messages/messages.jsonl")


def _message_log_bucket() -> str:
    cfg = _load_s3_config()
    return cfg["S3_BUCKET"]


def log_message(msg: Message, sender_name: Optional[str] = None) -> None:
    """Append a message to the S3 JSONL log.

    Args:
        msg:         The message to log.
        sender_name: Optional sender name from allowed_senders lookup.
    """
    entry = {
        "id": msg.id,
        "sender_number": msg.sender,
        "sender_name": sender_name,
        "body": msg.body,
        "received_at": msg.received_at,
    }
    body = json.dumps(entry) + "\n"

    bucket = _message_log_bucket()
    key = _message_log_key()

    # Read existing content, append new line, write back (S3 doesn't support append)
    try:
        existing = _s3_client().get_object(Bucket=bucket, Key=key)["Body"].read().decode()
    except _s3_client().exceptions.NoSuchKey:
        existing = ""

    _s3_client().put_object(Bucket=bucket, Key=key, Body=(existing + body).encode())


def load_messages_from_s3() -> Iterator[Message]:
    """Yield all messages from the S3 JSONL log.

    Used by Flask on startup to rebuild SQLite.
    """
    bucket = _message_log_bucket()
    key = _message_log_key()

    try:
        response = _s3_client().get_object(Bucket=bucket, Key=key)
        content = response["Body"].read().decode()
    except _s3_client().exceptions.NoSuchKey:
        return

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            yield Message(
                id=d["id"],
                sender=d["sender_number"],
                body=d["body"],
                received_at=d["received_at"],
            )
        except (KeyError, json.JSONDecodeError) as e:
            logger.warning("Skipping malformed S3 log line: %s (%s)", line[:100], e)


# ---------------------------------------------------------------------------
# Config snapshots
# ---------------------------------------------------------------------------

def _config_snapshot_prefix() -> str:
    cfg = _load_s3_config()
    return cfg.get("S3_CONFIG_PREFIX", "config/config")


def _config_snapshot_key(timestamp: str) -> str:
    return f"{_config_snapshot_prefix()}-{timestamp}.json"


def save_config_snapshot(config_dict: dict) -> None:
    """Save a timestamped config snapshot to S3 and prune old snapshots (keep 10)."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S")
    bucket = _config_snapshot_bucket()
    key = _config_snapshot_key(timestamp)

    _s3_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(config_dict, separators=(",", ":")).encode(),
        ContentType="application/json",
    )
    logger.info("Saved config snapshot to s3://%s/%s", bucket, key)

    _prune_config_snapshots(bucket)


def _config_snapshot_bucket() -> str:
    cfg = _load_s3_config()
    return cfg["S3_BUCKET"]


def _prune_config_snapshots(bucket: str) -> None:
    """Delete oldest config snapshots, keeping the 10 most recent."""
    prefix = _config_snapshot_prefix()
    response = _s3_client().list_objects_v2(Bucket=bucket, Prefix=prefix)

    keys = [obj["Key"] for obj in response.get("Contents", [])]
    if len(keys) <= 10:
        return

    # Sort by key (timestamp is in the key) and delete oldest
    keys.sort()
    for old_key in keys[: -10]:
        _s3_client().delete_object(Bucket=bucket, Key=old_key)
        logger.info("Pruned old config snapshot: s3://%s/%s", bucket, old_key)


def load_latest_config() -> Optional[dict]:
    """Load the most recent config snapshot from S3.

    Returns the parsed JSON dict, or None if no snapshot exists.
    """
    bucket = _config_snapshot_bucket()
    prefix = _config_snapshot_prefix()

    response = _s3_client().list_objects_v2(Bucket=bucket, Prefix=prefix)
    keys = [obj["Key"] for obj in response.get("Contents", [])]
    if not keys:
        return None

    # Most recent is the last in sorted order (timestamp in key)
    latest_key = sorted(keys)[-1]
    response = _s3_client().get_object(Bucket=bucket, Key=latest_key)
    content = response["Body"].read().decode()
    return json.loads(content)
