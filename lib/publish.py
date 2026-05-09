"""Publish messages and config JSON to Adafruit IO over MQTT.

Uses the standard MQTT topic format that Adafruit IO uses:
  {username}/feeds/{feedname}

Flask publishes via MQTT so the ESP32 can receive messages in real-time
via its existing MQTT subscription.
"""

import json
import logging
import tomllib
from pathlib import Path

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config (loaded from settings.toml)
# ---------------------------------------------------------------------------

def _mqtt_config() -> dict:
    """Load MQTT config from settings.toml."""
    settings_path = Path(__file__).parent.parent / "heart-sms-receiver" / "settings.toml"
    if not settings_path.exists():
        return {}
    with open(settings_path, "rb") as f:
        return tomllib.load(f)


# ---------------------------------------------------------------------------
# MQTT client factory
# ---------------------------------------------------------------------------

def _mqtt_client() -> mqtt.Client:
    """Build an MQTT client from settings.toml (credentials only)."""
    cfg = _mqtt_config()
    # Fall back to AIO_USERNAME/AIO_KEY if MQTT_USERNAME/PASSWORD not set
    username = cfg.get("MQTT_USERNAME") or cfg.get("AIO_USERNAME", "")
    password = cfg.get("MQTT_PASSWORD") or cfg.get("AIO_KEY", "")

    client = mqtt.Client()
    if username:
        client.username_pw_set(username, password)
    return client


# ---------------------------------------------------------------------------
# MQTT topic helpers
# ---------------------------------------------------------------------------

def _feed_topic(feed: str, username: str) -> str:
    """Build the Adafruit IO MQTT topic for a feed.

    Format: {username}/feeds/{feed}
    If feed already contains 'feeds/', use as-is.
    """
    if "/feeds/" in feed:
        return feed
    return f"{username}/feeds/{feed}"


# ---------------------------------------------------------------------------
# Publish message (called by Flask on each inbound SMS)
# ---------------------------------------------------------------------------

def publish_message(body: str, feed: str | None = None) -> bool:
    """Publish a message body to the AIO feed over MQTT.

    Args:
        body:     The SMS message text.
        feed:     Feed name. If None, loaded from settings.toml as AIO_FEED.

    Returns:
        True on success, False on failure.
    """
    cfg = _mqtt_config()
    feed = feed or cfg.get("AIO_FEED", "")
    username = cfg.get("AIO_USERNAME", "")

    if not feed or not username:
        logger.warning("MQTT/AIO_FEED not configured; skipping publish")
        return False

    host = cfg.get("MQTT_HOST", "io.adafruit.com")
    port = int(cfg.get("MQTT_PORT", 8883))
    topic = _feed_topic(feed, username)

    client = _mqtt_client()
    try:
        client.connect(host, port, keepalive=30)
        client.loop_start()
        result = client.publish(topic, body.encode(), qos=0)
        client.loop_stop()
        client.disconnect()
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.error("MQTT publish failed: rc=%s", result.rc)
            return False
        logger.info("Published to MQTT %s:%d/%s", host, port, topic)
        return True
    except Exception as e:
        logger.error("Failed to publish via MQTT: %s", e)
        return False


# ---------------------------------------------------------------------------
# Publish config JSON (called after any config change via admin UI)
# ---------------------------------------------------------------------------

def publish_config(config_dict: dict, feed: str | None = None) -> bool:
    """Publish config JSON to a feed over MQTT.

    Args:
        config_dict: The config dict to serialize and send.
        feed:        Feed name. If None, loaded from settings.toml as AIO_CONFIG_FEED.

    Returns:
        True on success, False on failure.
    """
    cfg = _mqtt_config()
    feed = feed or cfg.get("AIO_CONFIG_FEED", "")
    username = cfg.get("AIO_USERNAME", "")

    if not feed or not username:
        logger.warning("MQTT/AIO_CONFIG_FEED not configured; skipping config publish")
        return False

    host = cfg.get("MQTT_HOST", "io.adafruit.com")
    port = int(cfg.get("MQTT_PORT", 8883))
    topic = _feed_topic(feed, username)
    payload = json.dumps(config_dict, separators=(",", ":"))

    client = _mqtt_client()
    try:
        client.connect(host, port, keepalive=30)
        client.loop_start()
        result = client.publish(topic, payload.encode(), qos=0)
        client.loop_stop()
        client.disconnect()
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.error("MQTT config publish failed: rc=%s", result.rc)
            return False
        logger.info("Published config to MQTT %s:%d/%s", host, port, topic)
        return True
    except Exception as e:
        logger.error("Failed to publish config via MQTT: %s", e)
        return False
