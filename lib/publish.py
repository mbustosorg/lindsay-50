"""Publish config and message JSON to Adafruit IO over MQTT.

Used by Flask to push config changes and messages to Adafruit IO so the ESP32
subscribes via MQTT.
"""

import json
import logging

import paho.mqtt.client as mqtt

from lib_shared.config_reader import get_config
cfg = get_config()


logger = logging.getLogger(__name__)


def _feed_topic(feed: str, username: str) -> str:
    """Build the Adafruit IO MQTT topic for a feed."""
    if "/feeds/" in feed:
        return feed
    return f"{username}/feeds/{feed}"


def publish_config(config_dict: dict) -> bool:
    """Publish config JSON to the AIO config feed over MQTT.

    Returns True on success, False on failure.
    """
    host = cfg.AIO_HOST
    port = int(cfg.AIO_PORT)
    feed = cfg.AIO_CONFIG_FEED
    username = cfg.AIO_USERNAME
    password = cfg.AIO_KEY

    if not feed or not username:
        logger.warning("AIO not configured; skipping config publish")
        return False

    topic = _feed_topic(feed, username)

    payload = json.dumps({"value": _compact_json(config_dict)}, separators=(",", ":"))

    client = mqtt.Client(
        client_id=f"lindsay-flask-cfg-{id(None)}",
        clean_session=True,
    )
    if username:
        client.username_pw_set(username, password)

    try:
        client.connect(host, port, keepalive=30)
        client.loop_start()
        result = client.publish(topic, payload.encode(), qos=1)
        client.loop_stop()
        client.disconnect()
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.error("Config publish failed: rc=%s", result.rc)
            return False
        logger.info("Published config to MQTT %s:%d/%s", host, port, topic)
        return True
    except Exception as e:
        logger.error("Failed to publish config: %s", e)
        return False


def publish_message(body: str,
                   msg_id: str | None = None,
                   sender: str | None = None,
                   received_at: str | None = None) -> bool:
    """Publish a message JSON to the AIO feed over MQTT.

    Always uses MQTT regardless of host (local Mosquitto or Adafruit IO cloud).

    Args:
        body:        The SMS message text.
        msg_id:      Our generated UUID for this message.
        sender:      Sender phone number (E.164).
        received_at: ISO8601 timestamp.

    Returns:
        True on success, False on failure.
    """
    host = cfg.AIO_HOST
    port = int(cfg.AIO_PORT)
    username = cfg.AIO_USERNAME
    password = cfg.AIO_KEY
    feed = cfg.AIO_MESSAGES_FEED

    if not feed or not username:
        logger.warning("AIO not configured; skipping publish")
        return False

    topic = _feed_topic(feed, username)

    payload = json.dumps({
        "id": msg_id,
        "sender": sender,
        "body": body,
        "received_at": received_at,
    }, separators=(",", ":"))

    client = mqtt.Client(
        client_id=f"lindsay-flask-msg-{id(None)}",
        clean_session=True,
    )
    if username:
        client.username_pw_set(username, password)

    try:
        client.connect(host, port, keepalive=30)
        client.loop_start()
        result = client.publish(topic, payload.encode(), qos=1)
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


def _compact_json(d: dict) -> str:
    """Compact JSON serialization for Adafruit IO value field."""
    return json.dumps(d, separators=(",", ":"))
