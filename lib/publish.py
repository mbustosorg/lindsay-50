"""Publish config and message JSON to Adafruit IO over MQTT.

Used by Flask to push config changes and messages to Adafruit IO so the ESP32
subscribes via MQTT.

MQTT_PROVIDER determines the client backend:
  - "adafruit" : Adafruit_IO.MQTTClient (TLS on port 8883, Heroku)
  - "paho"     : paho.mqtt.client (plain TCP, local dev with Mosquitto)
"""

import json
import logging

from lib_shared.config_reader import get_config
cfg = get_config()
from lib_shared.models import MessageEnvelope

logger = logging.getLogger(__name__)


def _adafruit_publish(feed: str, payload: str) -> bool:
    """Publish using Adafruit_IO.MQTTClient."""
    from Adafruit_IO import MQTTClient
    username = cfg.AIO_USERNAME
    key = cfg.AIO_KEY

    client = MQTTClient(username, key, service_host=cfg.AIO_HOST, secure=True)
    try:
        client.connect()
        client.publish(feed, value=payload)
        client.disconnect()
        logger.info("Published to Adafruit IO MQTT %s/%s", username, feed)
        return True
    except Exception as e:
        logger.error("Adafruit IO MQTT publish failed: %s", e)
        return False


def _paho_publish(feed: str, payload: str) -> bool:
    """Publish using raw paho-mqtt.client."""
    import paho.mqtt.client as mqtt

    host = cfg.AIO_HOST
    port = int(cfg.AIO_PORT)
    username = cfg.AIO_USERNAME
    key = cfg.AIO_KEY

    if "/feeds/" in feed:
        topic = feed
    else:
        topic = f"{username}/feeds/{feed}"

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, clean_session=True)  # type: ignore[reportPrivateImportUsage]
    if username:
        client.username_pw_set(username, key)

    try:
        client.connect(host, port, keepalive=30)
        client.loop_start()
        result = client.publish(topic, payload.encode(), qos=1)
        client.loop_stop()
        client.disconnect()
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.error("MQTT publish failed: rc=%s", result.rc)
            return False
        logger.info("Published to MQTT %s:%d/%s qos=1 payload=%r", host, port, topic, payload)
        return True
    except Exception as e:
        logger.error("MQTT publish failed: %s", e)
        return False


def publish_config(config_dict: dict) -> bool:
    """Publish config JSON to the AIO config feed over MQTT.

    Returns True on success, False on failure.
    """
    feed = cfg.AIO_CONFIG_FEED
    if not feed:
        logger.warning("AIO_CONFIG_FEED not set; skipping config publish")
        return False

    payload = json.dumps({"value": json.dumps(config_dict, separators=(",", ":"))})

    if cfg.MQTT_CLIENT == "adafruit":
        return _adafruit_publish(feed, payload)
    else:
        return _paho_publish(feed, payload)


def publish_message(body: str,
                   msg_id: str | None = None,
                   sender: str | None = None,
                   received_at: str | None = None) -> bool:
    """Publish a message JSON to the AIO feed over MQTT.

    Args:
        body:        The SMS message text.
        msg_id:      Our generated UUID for this message.
        sender:      Sender phone number (E.164).
        received_at: ISO8601 timestamp.

    Returns:
        True on success, False on failure.
    """
    feed = cfg.AIO_MESSAGES_FEED
    if not feed:
        logger.warning("AIO_MESSAGES_FEED not set; skipping publish")
        return False

    payload = json.dumps({
        "id": msg_id,
        "sender": sender,
        "body": body,
        "received_at": received_at,
    }, separators=(",", ":"))

    if cfg.MQTT_CLIENT == "adafruit":
        return _adafruit_publish(feed, payload)
    else:
        return _paho_publish(feed, payload)


def publish_envelope(envelope: MessageEnvelope) -> bool:
    """Publish a MessageEnvelope to the unified AIO feed over MQTT.

    Returns True on success, False on failure.
    """
    feed = cfg.AIO_FEED
    if not feed:
        logger.warning("AIO_FEED not set; skipping envelope publish")
        return False

    payload = envelope.to_json()

    if cfg.MQTT_CLIENT == "adafruit":
        return _adafruit_publish(feed, payload)
    else:
        return _paho_publish(feed, payload)
