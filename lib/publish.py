"""Publish config JSON to Adafruit IO HTTP feed.

Used by Flask to push config changes to Adafruit IO so the ESP32
can fetch updated settings.
"""

import logging
import tomllib
from pathlib import Path

import requests

logger = logging.getLogger(__name__)


def _aio_config() -> dict:
    """Load Adafruit IO config from settings.toml."""
    settings_path = Path(__file__).parent.parent / "heart-sms-receiver" / "settings.toml"
    if not settings_path.exists():
        return {}
    with open(settings_path, "rb") as f:
        return tomllib.load(f)


def publish_config(config_dict: dict) -> bool:
    """Send config JSON to Adafruit IO HTTP feed.

    Returns True on success, False on failure.
    """
    cfg = _aio_config()
    username = cfg.get("AIO_USERNAME")
    key = cfg.get("AIO_KEY")
    feed = cfg.get("AIO_CONFIG_FEED")  # separate feed for config

    if not all([username, key, feed]):
        logger.warning("Adafruit IO config feed not configured; skipping publish")
        return False

    # Build full feed URL
    url = f"https://io.adafruit.com/api/v2/{username}/feeds/{feed}/data"
    headers = {"X-AIO-Key": key, "Content-Type": "application/json"}
    payload = {"value": _compact_json(config_dict)}

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info("Published config to Adafruit IO feed %s", feed)
        return True
    except Exception as e:
        logger.error("Failed to publish config to Adafruit IO: %s", e)
        return False


def _compact_json(d: dict) -> str:
    """Compact JSON serialization for Adafruit IO value field."""
    import json
    return json.dumps(d, separators=(",", ":"))
